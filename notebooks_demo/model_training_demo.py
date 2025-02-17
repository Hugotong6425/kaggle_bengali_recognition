# ---
# jupyter:
#   jupytext:
#     formats: py:light
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.3.3
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %load_ext autoreload
# %autoreload 2

# +
import os 
# os.environ["CUDA_VISIBLE_DEVICES"]="0"
import numpy as np
import wandb
# from apex import amp

import torch
import torch.optim as optim
from torch import nn
from torchvision import transforms
import torchcontrib
from sklearn.utils.class_weight import compute_class_weight

from hw_grapheme.io.load_data import load_processed_data
from hw_grapheme.model_archs.se_resnext import se_resnext50
from hw_grapheme.models.train import train_model
from hw_grapheme.train_utils.create_dataloader import create_dataloaders_train
from hw_grapheme.train_utils.train_test_split import stratified_split_kfold
import pickle
from pathlib import Path
# -

DATA_PATH = Path( "../data/processed/size_128")

# +
# load processed data 
pickle_paths = [
#     DATA_PATH/"sample.pickle",
    DATA_PATH/"train_data_0.pickle",
#     DATA_PATH/"train_data_1.pickle",
#     DATA_PATH/"train_data_2.pickle",
#     DATA_PATH/"train_data_3.pickle",
]

image_data, name_data, label_data = load_processed_data(pickle_paths, image_size=128)

# image_data = image_data[:500]
# name_data =name_data[:500]
# label_data = label_data[:500]

# with open ('../data/processed/size_128/sample.pickle','wb') as f:
#     pickle.dump((image_data, name_data, label_data), f)

# +
# split train valid set
n_splits = 5
random_seed = 2020

train_idx_list, test_idx_list = stratified_split_kfold(
    image_data, label_data, n_splits, random_seed
)
# -

# create data_transforms
data_transforms = {
    'train': transforms.Compose([
        transforms.ToPILImage(),
        # transforms.RandomAffine(degrees=10, scale=(1.0, 1.15)),
        transforms.Grayscale(num_output_channels=3),
        transforms.ToTensor(),
        # transforms.Normalize([0.0692], [0.2051]),
        # transforms.ToPILImage(),
    ]),
    'val': transforms.Compose([
        transforms.ToPILImage(),
        transforms.Grayscale(num_output_channels=3),
        transforms.ToTensor(),
        # transforms.Normalize([0.0692], [0.2051])
    ]),
}

# +
# default training setting
num_workers = 6
pin_memory = True
fold = list(range(n_splits))

# customize training setting
n_epoch = 120
batch_size = 128
mixed_precision = False

model_arch = se_resnext50
model_parameter = {}

swa = True

optimizer = optim.SGD
optimizer_parameter = {"weight_decay": 1e-4, "momentum": 0.9, "lr": 0.2, "nesterov": True}

# whether to use weighted loss for each class
is_weighted_class_loss = True

# create lr scheduler
epoch_scheduler_func = None
epoch_scheduler_func_para = {}
error_plateau_scheduler_func = optim.lr_scheduler.ReduceLROnPlateau
error_plateau_scheduler_func_para = {"mode": "min", "factor": 0.1, "patience": 10, "verbose": True, "min_lr":1e-3}

# prob. of using ["mixup", "cutmix", "cross_entropy"] loss
train_loss_prob = [0.5, 0.5, 0.0]
mixup_alpha = 0.4  # for mixup/cutmix only

# weighting of [root, vowel, consonant]
head_weights = [0.5, 0.25, 0.25]

wandb_log = True

# save dir, set None to not save, need to manual create folders first
save_dir = "../models/baseline-1080ti
Path(save_dir).mkdir(parents=True,exist_ok=True)
# save_dir = None
# -

    train_input_args = {
        "model": model, 
        "optimizer": optimizer_ft,
        "dataloaders": data_loaders,
        "mixed_precision": mixed_precision, 
        "train_loss_prob": train_loss_prob,
        "class_weights": class_weights,
        "head_weights": head_weights,
        "mixup_alpha": mixup_alpha, 
        "num_epochs": n_epoch,
        "epoch_scheduler": epoch_scheduler, 
        "error_plateau_scheduler": error_plateau_scheduler,
        "save_dir": full_save_dir,
        "wandb_log": wandb_log,
        "swa": swa,
    }




# +
if is_weighted_class_loss:
    root_label = label_data[:, 0]
    vowel_label = label_data[:, 1]
    consonant_label = label_data[:, 2]

    class_weight = "balanced"

    root_cls_weight = compute_class_weight(class_weight, np.unique(root_label), root_label)
    vowel_cls_weight = compute_class_weight(class_weight, np.unique(vowel_label), vowel_label)
    consonant_cls_weight = compute_class_weight(class_weight, np.unique(consonant_label), consonant_label)
    
    class_weights = [
        torch.Tensor(root_cls_weight).cuda(),
        torch.Tensor(vowel_cls_weight).cuda(),
        torch.Tensor(consonant_cls_weight).cuda(),
    ]
else:
    class_weights = None
    
for i, (train_idx, valid_idx) in enumerate(zip(train_idx_list, test_idx_list)):
    # skip unwanted fold
    if i not in [0]:
        continue
        
    print(f"Training fold {i}") 
        
    # create model 
    model = model_arch(**model_parameter)
    
    # create optimizer
    optimizer_ft = optimizer(model.parameters(), **optimizer_parameter)
    
    if swa:
        optimizer_ft = torchcontrib.optim.SWA(optimizer_ft)
        
    if mixed_precision:
        model.to("cuda")
        model, optimizer_ft = amp.initialize(model, optimizer_ft, opt_level="O1")
        model = nn.parallel.DataParallel(model)
    else:
        model.to("cuda")
        model = nn.DataParallel(model)

    # create data loader
    data_loaders = create_dataloaders_train(
        image_data, name_data, label_data, train_idx, valid_idx, 
        data_transforms, batch_size, num_workers, pin_memory
    )
    
    # create epoch_scheduler
    if epoch_scheduler_func:
        epoch_scheduler = epoch_scheduler_func(optimizer_ft, **epoch_scheduler_func_para)
    else:
        epoch_scheduler = None
        
    # create error_plateaus_scheduler
    if error_plateau_scheduler_func:
        error_plateau_scheduler = error_plateau_scheduler_func(optimizer_ft, **error_plateau_scheduler_func_para)
    else:
        error_plateau_scheduler = None
        
    # callbacks = {}
    if save_dir:
        full_save_dir = os.path.join(save_dir, f"fold_{i}")
    else:
        full_save_dir = None
        
    wandb.init(name='baseline-1080-ti-128', project='baseline')

    callbacks = train_model(**train_input_args)


# +
# configs = {
#     "model": "efficient 0",
#     "pretrain": pretrain,
#     "head_info": "1 fc",
#     "input_size": "224X224",
#     "optimizer": "adam",
#     "n_fold": n_splits,
#     "split_seed": random_seed,
#     "batch_size": batch_size,
#     "epoch": n_epoch,
#     "mixed_precision": mixed_precision
# }
