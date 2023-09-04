import torch
from torch import optim, nn
import torchinfo
import torchmetrics
import copy
import os
import sys
import tempfile
import uuid
import numpy as np
import collections
import argparse
import logging
from tqdm import tqdm
import yaml
from pathlib import Path
import openwakeword
from openwakeword.data import generate_adversarial_texts, augment_clips, mmap_batch_generator
from openwakeword.utils import compute_features_from_generator


# Base model class for an openwakeword model
class Model(nn.Module):
    def __init__(self, n_classes=1, input_shape=(16, 96), model_type="dnn",
                 layer_dim=128, seconds_per_example=None):
        super().__init__()

        # Store inputs as attributes
        self.n_classes = n_classes
        self.input_shape = input_shape
        self.seconds_per_example = seconds_per_example
        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        self.best_models = []
        self.best_val_fp = 1000
        self.best_val_accuracy = 0
        self.best_val_recall = 0
        self.best_train_recall = 0

        # Define model (currently on fully-connected network supported)
        if model_type == "dnn":
            self.model = nn.Sequential(
                nn.Flatten(),
                nn.Linear(input_shape[0]*input_shape[1], layer_dim),
                nn.LayerNorm(layer_dim),
                nn.ReLU(),
                nn.Linear(layer_dim, layer_dim),
                nn.LayerNorm(layer_dim),
                nn.ReLU(),
                nn.Linear(layer_dim, n_classes),
                nn.Sigmoid() if n_classes == 1 else nn.ReLU(),
            )
        elif model_type == "rnn":
            class Net(nn.Module):
                def __init__(self, input_shape, n_classes=1):
                    super().__init__()
                    self.layer1 = nn.LSTM(input_shape[-1], 64, num_layers=2, bidirectional=True,
                                          batch_first=True, dropout=0.0)
                    self.layer2 = nn.Linear(64*2, n_classes)
                    self.layer3 = nn.Sigmoid() if n_classes == 1 else nn.ReLU()

                def forward(self, x):
                    out, h = self.layer1(x)
                    return self.layer3(self.layer2(out[:, -1]))
            self.model = Net(input_shape, n_classes)

        # Define metrics
        if n_classes == 1:
            self.fp = lambda pred, y: (y-pred <= -0.5).sum()
            self.recall = torchmetrics.Recall(task='binary')
            self.accuracy = torchmetrics.Accuracy(task='binary')
        else:
            def multiclass_fp(p, y, threshold=0.5):
                probs = torch.nn.functional.softmax(p, dim=1)
                neg_ndcs = y == 0
                fp = (probs[neg_ndcs].argmax(axis=1) != 0 & (probs[neg_ndcs].max(axis=1)[0] > threshold)).sum()
                return fp

            def positive_class_recall(p, y, negative_class_label=0, threshold=0.5):
                probs = torch.nn.functional.softmax(p, dim=1)
                pos_ndcs = y != 0
                rcll = (probs[pos_ndcs].argmax(axis=1) > 0
                        & (probs[pos_ndcs].max(axis=1)[0] >= threshold)).sum()/pos_ndcs.sum()
                return rcll

            def positive_class_accuracy(p, y, negative_class_label=0):
                probs = torch.nn.functional.softmax(p, dim=1)
                pos_preds = probs.argmax(axis=1) != negative_class_label
                acc = (probs[pos_preds].argmax(axis=1) == y[pos_preds]).sum()/pos_preds.sum()
                return acc

            self.fp = multiclass_fp
            self.acc = positive_class_accuracy
            self.recall = positive_class_recall

        self.n_fp = 0
        self.val_fp = 0

        # Define logging dict (in-memory)
        self.history = collections.defaultdict(list)

        # Define optimizer and loss
        self.loss = torch.nn.functional.binary_cross_entropy if n_classes == 1 else nn.functional.cross_entropy
        self.optimizer = optim.Adam(self.model.parameters(), lr=0.0001)

    def save_model(self, output_path):
        """
        Saves the weights of a trained Pytorch model
        """
        if self.n_classes == 1:
            torch.save(self.model, output_path)

    def export_to_onnx(self, output_path, class_mapping=""):
        obj = self
        # Make simple model for export based on model structure
        if self.n_classes == 1:
            # Save ONNX model
            torch.onnx.export(self.model.to("cpu"), torch.rand(self.input_shape)[None, ], output_path,
                              output_names=[class_mapping])

        elif self.n_classes >= 1:
            class M(nn.Module):
                def __init__(self):
                    super().__init__()

                    # Define model
                    self.model = obj.model.to("cpu")

                def forward(self, x):
                    return torch.nn.functional.softmax(self.model(x), dim=1)

            # Save ONNX model
            torch.onnx.export(M(), torch.rand(self.input_shape)[None, ], output_path,
                              output_names=[class_mapping])

    def lr_warmup_cosine_decay(self,
                               global_step,
                               warmup_steps=0,
                               hold=0,
                               total_steps=0,
                               start_lr=0.0,
                               target_lr=1e-3
                               ):
        # Cosine decay
        learning_rate = 0.5 * target_lr * (1 + np.cos(np.pi * (global_step - warmup_steps - hold)
                                           / float(total_steps - warmup_steps - hold)))

        # Target LR * progress of warmup (=1 at the final warmup step)
        warmup_lr = target_lr * (global_step / warmup_steps)

        # Choose between `warmup_lr`, `target_lr` and `learning_rate` based on whether
        # `global_step < warmup_steps` and we're still holding.
        # i.e. warm up if we're still warming up and use cosine decayed lr otherwise
        if hold > 0:
            learning_rate = np.where(global_step > warmup_steps + hold,
                                     learning_rate, target_lr)

        learning_rate = np.where(global_step < warmup_steps, warmup_lr, learning_rate)
        return learning_rate

    def forward(self, x):
        return self.model(x)

    def summary(self):
        return torchinfo.summary(self.model, input_size=(1,) + self.input_shape)

    def average_models(self, models=None):
        """Averages the weights of the provided models together to make a new model"""

        if models is None:
            models = self.best_models

        # Clone a model from the list as the base for the averaged model
        averaged_model = copy.deepcopy(models[0])
        averaged_model_dict = averaged_model.state_dict()

        # Initialize a running total of the weights
        for key in averaged_model_dict:
            averaged_model_dict[key] *= 0  # set to 0

        for model in models:
            model_dict = model.state_dict()
            for key, value in model_dict.items():
                averaged_model_dict[key] += value

        for key in averaged_model_dict:
            averaged_model_dict[key] /= len(models)

        # Load the averaged weights into the model
        averaged_model.load_state_dict(averaged_model_dict)

        return averaged_model

    def auto_train(self, X_train, X_val, false_positive_val_data, steps=50000, max_negative_weight=1000,
                   target_val_accuracy=0.7, target_val_recall=0.5, target_val_fp_per_hour=0.2):
        """A sequence of training steps that produce relatively strong models
        automatically, based on validation data and performance targets provided.
        After training merges the best checkpoints and returns a single model.
        """

        # Get false positive validation data duration
        val_set_hrs = 11.3

        # Sequence 1
        print("Starting training sequence 1...")
        lr = 0.0001
        weights = np.linspace(1, max_negative_weight, int(steps)).tolist()
        val_steps = np.linspace(steps-int(steps*0.25), steps, 20).astype(np.int64)
        self.train_model(
                    X=X_train,
                    X_val=X_val,
                    false_positive_val_data=false_positive_val_data,
                    max_steps=steps,
                    negative_weight_schedule=weights,
                    val_steps=val_steps, warmup_steps=steps//5,
                    hold_steps=steps//3, lr=lr, max_val_fp_per_hr=target_val_fp_per_hour, val_set_hrs=val_set_hrs,
                    target_val_accuracy=target_val_accuracy, target_val_recall=target_val_recall)

        # Sequence 2
        print("Starting training sequence 2...")
        lr = lr/10
        steps = steps/10

        # Adjust weights as needed based on false positive per hour performance from first sequence
        if self.best_val_fp > target_val_fp_per_hour:
            max_negative_weight = max_negative_weight*2
            print("Increasing weight on negative examples to reduce false positives...")

        weights = np.linspace(1, max_negative_weight, int(steps)).tolist()
        val_steps = np.linspace(1, steps, 20).astype(np.int16)
        self.train_model(
                    X=X_train,
                    X_val=X_val,
                    false_positive_val_data=false_positive_val_data,
                    max_steps=steps,
                    negative_weight_schedule=weights,
                    val_steps=val_steps, warmup_steps=steps//5,
                    hold_steps=steps//3, lr=lr, max_val_fp_per_hr=target_val_fp_per_hour, val_set_hrs=val_set_hrs,
                    target_val_accuracy=target_val_accuracy, target_val_recall=target_val_recall)

        # Sequence 3
        print("Starting training sequence 3...")
        lr = lr/10

        # Adjust weights as needed based on false positive per hour performance from second sequence
        if self.best_val_fp > target_val_fp_per_hour:
            max_negative_weight = max_negative_weight*2
            print("Increasing weight on negative examples to reduce false positives...")

        weights = np.linspace(1, max_negative_weight, int(steps)).tolist()
        val_steps = np.linspace(1, steps, 20).astype(np.int16)
        self.train_model(
                    X=X_train,
                    X_val=X_val,
                    false_positive_val_data=false_positive_val_data,
                    max_steps=steps,
                    negative_weight_schedule=weights,
                    val_steps=val_steps, warmup_steps=steps//5,
                    hold_steps=steps//3, lr=lr, max_val_fp_per_hr=target_val_fp_per_hour, val_set_hrs=val_set_hrs,
                    target_val_accuracy=target_val_accuracy, target_val_recall=target_val_recall)

        # Merge best models
        print("Merging best checkpoints into single model...")
        combined_model = self.average_models(models=self.best_models)

        # Report validationmetrics for combined model
        with torch.no_grad():
            for batch in X_val:
                x, y = batch[0].to(self.device), batch[1].to(self.device)
                val_ps = combined_model(x)

            combined_model_recall = self.recall(val_ps, y[..., None]).detach().cpu().numpy()
            combined_model_accuracy = self.accuracy(val_ps, y[..., None].to(torch.int64)).detach().cpu().numpy()

            combined_model_fp = 0
            for batch in false_positive_val_data:
                x_val, y_val = batch[0].to(self.device), batch[1].to(self.device)
                val_ps = combined_model(x_val)
                combined_model_fp += self.fp(val_ps, y_val[..., None])

            combined_model_fp_per_hr = (combined_model_fp/val_set_hrs).detach().cpu().numpy()

        print("\n################\n")
        print("Final Model Accuracy:", combined_model_accuracy)
        print("Final Model Recall:", combined_model_recall)
        print("Final Model False Positives per Hour:", combined_model_fp_per_hr)
        print("\n################\n")

        return combined_model

    def export_model(self, model, model_name, output_dir):
        """Saves the trained openwakeword model to both onnx and tflite formats"""

        if self.n_classes != 1:
            raise ValueError("Exporting models to both onnx and tflite with more than one class is currently not supported! "
                             "Use the `export_to_onnx` function instead.")

        # Save ONNX model
        model_to_save = copy.deepcopy(model)
        torch.onnx.export(model_to_save.to("cpu"), torch.rand(self.input_shape)[None, ], os.path.join(output_dir, model_name + ".onnx"))

        # Convert to tflite from onnx model
        import onnx
        from onnx_tf.backend import prepare
        import tensorflow as tf

        # package versions
        # tensorflow==2.8.1
        # tensorflow_probability==0.16.0
        # protobuf==3.20
        # onnx_tf==1.10.0
        # onnx==1.14.0

        onnx_model = onnx.load(os.path.join(output_dir, model_name + ".onnx"))
        tf_rep = prepare(onnx_model, device="CPU")
        with tempfile.TemporaryDirectory() as tmp_dir:
            tf_rep.export_graph(os.path.join(tmp_dir, "tf_model"))

            converter = tf.lite.TFLiteConverter.from_saved_model(os.path.join(tmp_dir, "tf_model"))
            tflite_model = converter.convert()

            with open(os.path.join(output_dir, model_name + ".tflite"), 'wb') as f:
                f.write(tflite_model)

        return None

    def train_model(self, X, max_steps, warmup_steps, hold_steps, X_val=None,
                    false_positive_val_data=None,
                    negative_weight_schedule=[1],
                    val_steps=[250], lr=0.0001, max_val_fp_per_hr=0.1, target_val_accuracy=0.7, target_val_recall=0.5, val_set_hrs=1):
        # Move models and main class to target device
        self.to(self.device)
        self.model.to(self.device)

        # Train model
        accumulation_steps = 1
        accumulated_samples = 0
        for step_ndx, data in tqdm(enumerate(X, 0), total=max_steps, desc="Training"):
            # get the inputs; data is a list of [inputs, labels]
            x, y = data[0].to(self.device), data[1].to(self.device)
            y_ = y[..., None].to(torch.float32)

            # Update learning rates
            for g in self.optimizer.param_groups:
                g['lr'] = self.lr_warmup_cosine_decay(step_ndx, warmup_steps=warmup_steps, hold=hold_steps,
                                                      total_steps=max_steps, target_lr=lr)

            # zero the parameter gradients
            self.optimizer.zero_grad()

            # Get predictions for batch
            predictions = self.model(x)

            # Construct batch with only samples that have high loss
            neg_high_loss = predictions[(y == 0) & (predictions.squeeze() >= 0.001)]  # thresholds were chosen arbitrarily but work well
            pos_high_loss = predictions[(y == 1) & (predictions.squeeze() < 0.999)]
            y = torch.cat((y[(y == 0) & (predictions.squeeze() >= 0.001)], y[(y == 1) & (predictions.squeeze() < 0.999)]))
            y_ = y[..., None].to(torch.float32)
            predictions = torch.cat((neg_high_loss, pos_high_loss))

            # Set weights for batch
            if len(negative_weight_schedule) == 1:
                w = torch.ones(y.shape[0])*negative_weight_schedule[0]
                pos_ndcs = y == 1
                w[pos_ndcs] = 1
                w = w[..., None]
            else:
                if self.n_classes == 1:
                    w = torch.ones(y.shape[0])*negative_weight_schedule[step_ndx]
                    pos_ndcs = y == 1
                    w[pos_ndcs] = 1
                    w = w[..., None]

            # Do backpropagation, with gradient accumulation if the batch-size after selecting high loss examples is too small
            loss = self.loss(predictions, y_ if self.n_classes == 1 else y, w.to(self.device))
            loss = loss/accumulation_steps
            accumulated_samples += predictions.shape[0]
            if accumulated_samples < 128:
                accumulation_steps += 1
            else:
                loss.backward()
                self.optimizer.step()
                accumulation_steps = 1
                accumulated_samples = 0

            # Compute training metrics and log them
            fp = self.fp(predictions, y_ if self.n_classes == 1 else y)
            self.n_fp += fp

            self.history["loss"].append(loss.detach().cpu().numpy())
            self.history["recall"].append(self.recall(predictions, y_).detach().cpu().numpy())
            if self.n_classes != 1:
                self.history["accuracy"].append(self.acc(predictions, y).detach().cpu().numpy())

            # Run validation and log validation metrics
            if step_ndx in val_steps and step_ndx > 1 and false_positive_val_data is not None:
                # Get false positives per hour with false positive data
                val_fp = 0
                for val_step_ndx, data in enumerate(false_positive_val_data):
                    with torch.no_grad():
                        x_val, y_val = data[0].to(self.device), data[1].to(self.device)
                        val_predictions = self.model(x_val)
                        val_fp += self.fp(val_predictions, y_val[..., None])
                val_fp_per_hr = (val_fp/val_set_hrs).detach().cpu().numpy()
                self.history["val_fp_per_hr"].append(val_fp_per_hr)

            if step_ndx in val_steps and step_ndx > 1 and X_val is not None:
                # Get accuracy for balanced test examples of positive and negative clips
                for val_step_ndx, data in enumerate(X_val):
                    with torch.no_grad():
                        x_val, y_val = data[0].to(self.device), data[1].to(self.device)
                        val_predictions = self.model(x_val)
                        val_recall = self.recall(val_predictions, y_val[..., None]).detach().cpu().numpy()
                        val_acc = self.accuracy(val_predictions, y_val[..., None].to(torch.int64))
                self.history["val_accuracy"].append(val_acc.detach().cpu().numpy())
                self.history["val_recall"].append(val_recall)

                # Save models with a validation score below a given threshold
                print(val_fp_per_hr, self.history["val_accuracy"][-1], self.history["val_recall"][-1])
                if val_fp_per_hr <= max(self.best_val_fp, max_val_fp_per_hr) and \
                        self.history["val_accuracy"][-1] >= target_val_accuracy and \
                        self.history["val_recall"][-1] >= target_val_recall:
                    print("Saving checkpoint with metrics >= to targets!")
                    self.best_models.append(copy.deepcopy(self.model))
                    self.best_val_fp = val_fp_per_hr
                    self.best_val_recall = self.history["val_recall"][-1]
                    self.best_val_accuracy = self.history["val_accuracy"][-1]

            if step_ndx == max_steps-1:
                break


if __name__ == '__main__':
    # Get training config file
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--training_config",
        help="The path to the training config file",
        type=str,
        required=True
    )
    parser.add_argument(
        "--generate_clips",
        help="Execute the synthetic data generation process",
        type=bool,
        action="store_true",
        default="False",
        required=False
    )
    parser.add_argument(
        "--augment_clips",
        help="Execute the synthetic data augmentation process",
        type=bool,
        action="store_true",
        default="False",
        required=False
    )
    parser.add_argument(
        "--train_model",
        help="Execute the model training process",
        type=bool,
        action="store_true",
        default="False",
        required=False
    )

    args = parser.parse_args()
    config = yaml.load(open(args.training_config, 'r').read(), yaml.Loader)

    # imports Piper for synthetic sample generation
    sys.path.insert(0, os.path.abspath(config["piper_sample_generator_path"]))
    from generate_samples import generate_samples

    # Define output locations
    if not os.path.exists(config["output_dir"]):
        os.mkdir(config["output_dir"])

    positive_train_output_dir = os.path.join(config["output_dir"], config["model_name"], "positive_train")
    positive_test_output_dir = os.path.join(config["output_dir"], config["model_name"], "positive_test")
    negative_train_output_dir = os.path.join(config["output_dir"], config["model_name"], "negative_train")
    negative_test_output_dir = os.path.join(config["output_dir"], config["model_name"], "negative_test")
    feature_save_dir = os.path.join(config["output_dir"], config["model_name"])

    # Get paths for impulse response and background audio files
    rir_paths = [i.path for j in config["rir_paths"] for i in os.scandir(j)]
    background_paths = [i.path for j in config["background_paths"] for i in os.scandir(j)]

    if args.generate_clips is True:
        # Generate positive clips for training
        n_current_samples = len(os.listdir(positive_train_output_dir))
        if n_current_samples <= 0.95*config["n_samples"]:
            generate_samples(
                text=[config["target_phrase"]], max_samples=config["n_samples"]-n_current_samples,
                batch_size=config["tts_batch_size"],
                noise_scales=[0.98], noise_scale_ws=[0.98], length_scales=[0.75, 1.0, 1.25],
                output_dir=positive_train_output_dir, auto_reduce_batch_size=True,
                file_names=[uuid.uuid4().hex + ".wav" for i in range(config["n_samples"])]
            )
            torch.cuda.empty_cache()
        else:
            logging.warning(f"Skipping generation of positive clips for training, as ~{config['n_samples']} already exist")

        # Generate positive clips for testing
        n_current_samples = len(os.listdir(positive_test_output_dir))
        if n_current_samples <= 0.95*config["n_samples_val"]:
            generate_samples(text=[config["target_phrase"]], max_samples=config["n_samples_val"]-n_current_samples,
                            batch_size=config["tts_batch_size"],
                            noise_scales=[1.0], noise_scale_ws=[1.0], length_scales=[0.75, 1.0, 1.25],
                            output_dir=positive_test_output_dir, auto_reduce_batch_size=True)
            torch.cuda.empty_cache()
        else:
            logging.warning(f"Skipping generation of positive clips testing, as ~{config['n_samples_val']} already exist")

        # Generate adversarial negative clips for training
        n_current_samples = len(os.listdir(negative_train_output_dir))
        if n_current_samples <= 0.95*config["n_samples"]:
            adversarial_texts = config["custom_negative_phrases"]
            for target_phrase in config["target_phrase"]:
                adversarial_texts.append(generate_adversarial_texts(
                    input_text=target_phrase,
                    N=config["n_samples"]//len(config["target_phrase"]),
                    include_partial_phrase=1.0,
                    include_input_words=0.2))
            generate_samples(text=adversarial_texts, max_samples=config["n_samples"]-n_current_samples,
                            batch_size=config["tts_batch_size"]//7,
                            noise_scales=[0.98], noise_scale_ws=[0.98], length_scales=[0.75, 1.0, 1.25],
                            output_dir=negative_train_output_dir, auto_reduce_batch_size=True,
                            file_names=[uuid.uuid4().hex + ".wav" for i in range(config["n_samples"])]
                            )
            torch.cuda.empty_cache()
        else:
            logging.warning(f"Skipping generation of negative clips for training, as ~{config['n_samples']} already exist")

        # Generate adversarial negative clips for testing
        n_current_samples = len(os.listdir(negative_test_output_dir))
        if n_current_samples <= 0.95*config["n_samples_val"]:
            adversarial_texts = config["custom_negative_phrases"]
            for target_phrase in config["target_phrase"]:
                adversarial_texts.append(generate_adversarial_texts(
                    input_text=target_phrase,
                    N=config["n_samples_val"]//len(config["target_phrase"]),
                    include_partial_phrase=1.0,
                    include_input_words=0.2))
            generate_samples(text=adversarial_texts, max_samples=config["n_samples_val"]-n_current_samples,
                            batch_size=config["tts_batch_size"]//7,
                            noise_scales=[1.0], noise_scale_ws=[1.0], length_scales=[0.75, 1.0, 1.25],
                            output_dir=negative_test_output_dir, auto_reduce_batch_size=True)
            torch.cuda.empty_cache()
        else:
            logging.warning(f"Skipping generation of negative clips for testing, as ~{config['n_samples_val']} already exist")

    # Do Data Augmentation
    if args.augment_clips is True:
        if not os.path.exists(os.path.join(feature_save_dir, "positive_features_train.npy")):
            logging.info("Augmenting generated clips...")

            positive_clips_train = [str(i) for i in Path(positive_train_output_dir).glob("*.wav")]*config["augmentation_rounds"]
            positive_clips_train_generator = augment_clips(positive_clips_train, total_length=config["total_length"],
                                                        batch_size=config["augmentation_batch_size"],
                                                        background_clip_paths=background_paths,
                                                        RIR_paths=rir_paths)

            positive_clips_test = [str(i) for i in Path(positive_test_output_dir).glob("*.wav")]*config["augmentation_rounds"]
            positive_clips_test_generator = augment_clips(positive_clips_test, total_length=config["total_length"],
                                                        batch_size=config["augmentation_batch_size"],
                                                        background_clip_paths=background_paths,
                                                        RIR_paths=rir_paths)

            negative_clips_train = [str(i) for i in Path(negative_train_output_dir).glob("*.wav")]*config["augmentation_rounds"]
            negative_clips_train_generator = augment_clips(negative_clips_train, total_length=config["total_length"],
                                                        batch_size=config["augmentation_batch_size"],
                                                        background_clip_paths=background_paths,
                                                        RIR_paths=rir_paths)

            negative_clips_test = [str(i) for i in Path(negative_test_output_dir).glob("*.wav")]*config["augmentation_rounds"]
            negative_clips_test_generator = augment_clips(negative_clips_test, total_length=config["total_length"],
                                                        batch_size=config["augmentation_batch_size"],
                                                        background_clip_paths=background_paths,
                                                        RIR_paths=rir_paths)

            # Compute features and save to disk via memmapped arrays
            logging.info("Computer openwakeword features for generated samples...")
            n_cpus = os.cpu_count()
            if n_cpus is None:
                n_cpus = 1
            else:
                n_cpus = n_cpus//2
            compute_features_from_generator(positive_clips_train_generator, n_total=len(os.listdir(positive_train_output_dir)),
                                            clip_duration=config["total_length"],
                                            output_file=os.path.join(feature_save_dir, "positive_features_train.npy"),
                                            device="gpu" if torch.cuda.is_available() else "cpu",
                                            ncpu=n_cpus if not torch.cuda_is_available() else 1)

            compute_features_from_generator(negative_clips_train_generator, n_total=len(os.listdir(negative_train_output_dir)),
                                            clip_duration=config["total_length"],
                                            output_file=os.path.join(feature_save_dir, "negative_features_train.npy"),
                                            device="gpu" if torch.cuda.is_available() else "cpu",
                                            ncpu=n_cpus if not torch.cuda_is_available() else 1)

            compute_features_from_generator(positive_clips_test_generator, n_total=len(os.listdir(positive_test_output_dir)),
                                            clip_duration=config["total_length"],
                                            output_file=os.path.join(feature_save_dir, "positive_features_test.npy"),
                                            device="gpu" if torch.cuda.is_available() else "cpu",
                                            ncpu=n_cpus if not torch.cuda_is_available() else 1)

            compute_features_from_generator(negative_clips_test_generator, n_total=len(os.listdir(negative_test_output_dir)),
                                            clip_duration=config["total_length"],
                                            output_file=os.path.join(feature_save_dir, "negative_features_test.npy"),
                                            device="gpu" if torch.cuda.is_available() else "cpu",
                                            ncpu=n_cpus if not torch.cuda_is_available() else 1)
        else:
            logging.warning("Openwakeword features already exist, skipping data augmentation and feature generation")

    # Create openwakeword model
    if args.train_model is True:
        F = openwakeword.utils.AudioFeatures(device='cpu')
        input_shape = F.get_embedding_shape(config["total_length"]//16000)  # training data is always 16 khz

        oww = Model(n_classes=1, input_shape=input_shape, model_type=config["model_type"],
                    layer_dim=config["layer_size"], seconds_per_example=1280*input_shape[0]/16000)

        # Create data and label transform functions for batch generation
        def f(x, n=16):
            """Simple transformation function to ensure negative data is the appropriate shape for the model size"""
            n_chunks = x.shape[1]//n
            stacked = np.vstack((
                [x[:, i:i+n, :] for i in range(n_chunks)]
            ))
            return stacked

        data_transforms = {key: f for key in config["negative_data_files"].keys()}
        label_transforms = {key: lambda x: [1 for i in x] if key == "positive" else lambda x: [0 for i in x]
                            for key in ["positive"] + config["negative_data_files"] + ["adversarial_negative"]}

        # Make PyTorch data loaders for training and validation data
        batch_generator = mmap_batch_generator(
            config["feature_data_files"],
            n_per_class=config["batch_n_per_class"],
            data_transform_funcs=data_transforms,
            label_transform_funcs=label_transforms
        )

        class IterDataset(torch.utils.data.IterableDataset):
            def __init__(self, generator):
                self.generator = generator

            def __iter__(self):
                return self.generator

        X_train = torch.utils.data.DataLoader(IterDataset(batch_generator),
                                            batch_size=None, num_workers=8, prefetch_factor=16)

        X_val_fp = np.load(config["false_positive_validation_data_path"])
        X_val_fp = np.array([X_val_fp[i:i+input_shape[0]] for i in range(0, X_val_fp.shape[0]-input_shape[0], 1)])  # reshape to match model
        X_val_fp_labels = np.zeros(X_val_fp.shape[0]).astype(np.float32)
        X_val_fp = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(torch.from_numpy(X_val_fp), torch.from_numpy(X_val_fp_labels)),
            batch_size=len(X_val_fp_labels)
        )

        X_val = np.vstack((
            np.load(os.path.join(feature_save_dir, "positive_features_test.npy")),
            np.load(os.path.join(feature_save_dir, "negative_features_test.npy"))
        ))
        labels = np.hstack((np.ones(X_val.shape[0]//2), np.zeros(X_val.shape[0]//2))).astype(np.float32)

        X_val = torch.utils.data.DataLoader(
            torch.utils.data.TensorDataset(torch.from_numpy(X_val), torch.from_numpy(labels)),
            batch_size=len(labels)
        )

        # Run auto training and save model
        steps = 100000
        max_neg_weight = 1500
        target_accuracy = 0.7
        target_recall = 0.5
        target_fp_per_hour = 0.2

        # Run auto training
        best_model = oww.auto_train(
            X_train=X_train,
            X_val=X_val,
            false_positive_val_data=X_val_fp,
            steps=config["steps"],
            max_negative_weight=config["max_negative_weight"],
            target_val_accuracy=config["target_accuracy"],
            target_val_recall=config["target_recall"],
            target_val_fp_per_hour=config["target_fp_per_hour"]
        )

        # Export the trained model to onnx and tflite formats
        oww.export_model(model=best_model, model_name=config["model_name"], output_dir=config["output_dir"])
