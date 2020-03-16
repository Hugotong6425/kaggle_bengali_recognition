import os
import time
import torch
import wandb

import numpy as np
import pandas as pd

from torch import nn
import torch.nn.functional as F

from tqdm import tqdm_notebook
from torch.nn.utils import clip_grad_norm_


from hw_grapheme.callbacks.CallbackRecorder import CallbackRecorder
from hw_grapheme.callbacks.ExportLogger import ExportLogger
from hw_grapheme.train_utils.loss_func import (
    no_extra_augmentation_criterion_with_last_column,
    no_extra_augmentation_criterion_with_last_column_valid_only,
    cutmix_criterion_with_last_column,
    mixup_criterion_with_last_column,
    ohem_loss,
)

device = "cuda"

##### for cutmix training
def rand_bbox(size, lam):
    W = size[2]
    H = size[3]
    cut_rat = min(np.sqrt(1.0 - lam), 0.2)
    cut_w = np.int(W * cut_rat)
    cut_h = np.int(H * cut_rat)

    # uniform
    cx = np.random.randint(W)
    cy = np.random.randint(H)

    bbx1 = np.clip(cx - cut_w // 2, 0, W)
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = np.clip(cx + cut_w // 2, 0, W)
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return bbx1, bby1, bbx2, bby2


def cutmix_data(data, targets1, targets2, targets3, targets4, alpha):
    size = data.size(0)
    indices = torch.randperm(size)
    shuffled_data = data[indices]
    shuffled_targets1 = targets1[indices]
    shuffled_targets2 = targets2[indices]
    shuffled_targets3 = targets3[indices]
    shuffled_targets4 = targets4[indices]

    torch_beta = torch.distributions.Beta(alpha, alpha)
    lam = torch_beta.sample_n(size)
    # Remove duplicate case
    lam = torch.stack([lam, 1 - lam]).max(0)[0]
    # lam = lam.view(-1, 1, 1, 1)
    for i, (indice_i, lam_i) in enumerate(zip(indices, lam)):
        bbx1, bby1, bbx2, bby2 = rand_bbox(data.size(), lam_i)
        data[i, :, bbx1:bbx2, bby1:bby2] = data[i, :, bbx1:bbx2, bby1:bby2]
        # adjust lambda to exactly match pixel ratio
        lam[i] = 1 - (
            (bbx2 - bbx1) * (bby2 - bby1) / (data.size()[-1] * data.size()[-2])
        )
    lam = lam.to(device)

    targets = [
        targets1,
        shuffled_targets1,
        targets2,
        shuffled_targets2,
        targets3,
        shuffled_targets3,
        targets4,
        shuffled_targets4,
        lam,
    ]
    return data, targets


def mixup_data(data, targets1, targets2, targets3, targets4, alpha):
    size = data.size(0)
    indices = torch.randperm(size)
    shuffled_data = data[indices]
    shuffled_targets1 = targets1[indices]
    shuffled_targets2 = targets2[indices]
    shuffled_targets3 = targets3[indices]
    shuffled_targets4 = targets4[indices]

    torch_beta = torch.distributions.Beta(alpha, alpha)
    lam = torch_beta.sample_n(size)
    # Remove duplicate case
    lam = torch.stack([lam, 1 - lam]).max(0)[0].to(device)
    lam = lam.view(-1, 1, 1, 1)
    data = data * lam + shuffled_data * (1 - lam)
    targets = [
        targets1,
        shuffled_targets1,
        targets2,
        shuffled_targets2,
        targets3,
        shuffled_targets3,
        targets4,
        shuffled_targets4,
        lam,
    ]
    return data, targets


def train_phrase(
    model,
    optimizer,
    train_dataloader,
    mixed_precision,
    train_loss_prob_2,
    extra_augmentation_prob,
    head_weights,
    class_weights,
    mixup_alpha=0.4,
    cutmix_alpha=1,
    ohem_rate=0.7,
    batch_scheduler=None,
    wandb_log=False,
    start_swa=False,
):
    if mixed_precision:
        from apex import amp

    recorder = CallbackRecorder()
    model.train()  # Set model to training mode

    # Iterate over data.
    for i, (images, root, vowel, consonant, grapheme) in enumerate(
        tqdm_notebook(train_dataloader)
    ):
        images = images.to("cuda")
        root = root.long().to("cuda")
        vowel = vowel.long().to("cuda")
        consonant = consonant.long().to("cuda")
        grapheme = grapheme.long().to("cuda")

        # forward with all root vowel consonant outputs
        # with torch.set_grad_enabled(True):

        # determine whether loss function to use
        train_loss_2_choice_list = ["cross_entropy", "ohem"]
        train_loss_2 = np.random.choice(
            train_loss_2_choice_list, 1, p=train_loss_prob_2
        )

        if train_loss_2 == ["cross_entropy"]:
            loss_criteria = F.cross_entropy

            # currently only cross_entropy loss support weighted class loss
            if class_weights:
                root_class_weights = class_weights[0]
                vowel_class_weights = class_weights[1]
                consonant_class_weights = class_weights[2]
                grapheme_class_weights = class_weights[3]
            else:
                root_class_weights = None
                vowel_class_weights = None
                consonant_class_weights = None
                grapheme_class_weights = None

            # store the args pass into loas_criteria for each head
            loss_criteria_paras = {
                "root": {"weight": root_class_weights, "reduction": "none"},
                "vowel": {"weight": vowel_class_weights, "reduction": "none"},
                "consonant": {"weight": consonant_class_weights, "reduction": "none"},
                "grapheme": {"weight": grapheme_class_weights, "reduction": "none"},
            }
        elif train_loss_2 == ["ohem"]:
            loss_criteria = ohem_loss

            # store the args pass into loas_criteria for each head
            loss_criteria_paras = {
                "root": {"ohem_rate": ohem_rate},
                "vowel": {"ohem_rate": ohem_rate},
                "consonant": {"ohem_rate": ohem_rate},
                "grapheme": {"ohem_rate": ohem_rate},
            }
        else:
            print("ERROR in train phrase train_loss_2.")

        # determine whether extra img augmentation is used
        extra_augmentation_choice_list = ["mixup", "cutmix", "none"]
        extra_augmentation = np.random.choice(
            extra_augmentation_choice_list, 1, p=extra_augmentation_prob
        )

        if extra_augmentation == ["mixup"]:
            images, targets = mixup_data(images, root, vowel, consonant, grapheme, mixup_alpha)
            root_logit, vowel_logit, consonant_logit, grapheme_logit = model(images)
            loss = mixup_criterion_with_last_column(
                root_logit,
                vowel_logit,
                consonant_logit,
                grapheme_logit,
                targets,
                loss_criteria,
                loss_criteria_paras,
                head_weights=head_weights,
            )
        elif extra_augmentation == ["cutmix"]:
            images, targets = cutmix_data(images, root, vowel, consonant, grapheme, cutmix_alpha)
            root_logit, vowel_logit, consonant_logit, grapheme_logit = model(images)
            loss = cutmix_criterion_with_last_column(
                root_logit,
                vowel_logit,
                consonant_logit,
                grapheme_logit,
                targets,
                loss_criteria,
                loss_criteria_paras,
                head_weights=head_weights,
            )
        elif extra_augmentation == ["none"]:
            root_logit, vowel_logit, consonant_logit, grapheme_logit = model(images)
            targets = (root, vowel, consonant, grapheme)
            loss = no_extra_augmentation_criterion_with_last_column(
                root_logit,
                vowel_logit,
                consonant_logit,
                grapheme_logit,
                targets,
                loss_criteria,
                loss_criteria_paras,
                head_weights=head_weights,
            )
        else:
            print("ERROR in train phrase extra augmentation.")

        # zero the parameter gradients
        optimizer.zero_grad()

        # backward + optimize
        if mixed_precision:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
            clip_grad_norm_(amp.master_params(optimizer), 0.25)
        else:
            loss.backward()
            clip_grad_norm_(model.parameters(), 0.25)

        # Step LR scheudler before loss
        if batch_scheduler:
            if start_swa:
                batch_scheduler.step()

        optimizer.step()

        recorder.update(
            loss,
            root_logit,
            vowel_logit,
            consonant_logit,
            root.data,
            vowel.data,
            consonant.data,
        )

    recorder.evaluate()

    if wandb_log:
        recorder.wandb_log(phrase="train")

    return recorder

def validate_phrase(model, valid_dataloader, wandb_log=True, phrase="val"):
    recorder = CallbackRecorder()

    # Each epoch has a training and validation phase
    model.eval()  # Set model to evaluate mode

    # Iterate over data.
    for images, root, vowel, consonant, grapheme in tqdm_notebook(valid_dataloader):
        images = images.to("cuda")
        root = root.long().to("cuda")
        vowel = vowel.long().to("cuda")
        consonant = consonant.long().to("cuda")
        grapheme = grapheme.long().to("cuda")

        # forward
        # track history if only in train
        with torch.no_grad():
            root_logit, vowel_logit, consonant_logit, grapheme_logit = model(images)
            targets = (root, vowel, consonant, grapheme)

            # default use class weighted cross entropy loss for val set
            loss_criteria = F.cross_entropy
            loss_criteria_paras = {
                "root": {"weight": None, "reduction": "none"},
                "vowel": {"weight": None, "reduction": "none"},
                "consonant": {"weight": None, "reduction": "none"},
                "grapheme": {"weight": None, "reduction": "none"},
            }

            loss = no_extra_augmentation_criterion_with_last_column_valid_only(
                root_logit,
                vowel_logit,
                consonant_logit,
                grapheme_logit,
                targets,
                loss_criteria,
                loss_criteria_paras,
                head_weights=[0.5, 0.25, 0.25, 0.0],
            )

        recorder.update(
            loss,
            root_logit,
            vowel_logit,
            consonant_logit,
            root.data,
            vowel.data,
            consonant.data,
        )

    recorder.evaluate()

    if wandb_log:
        recorder.wandb_log(phrase=phrase)

    return recorder


def train_model(
    model,
    optimizer,
    dataloaders,
    mixed_precision,
    train_loss_prob_2,
    extra_augmentation_prob,
    class_weights=None,
    head_weights=[0.5, 0.25, 0.25],
    mixup_alpha=0.4,
    cutmix_alpha=1,
    ohem_rate=0.7,
    num_epochs=25,
    epoch_scheduler=None,
    error_plateau_scheduler=None,
    batch_scheduler=None,
    save_dir=None,
    wandb_log=False,
    swa=False,
):
    """
    class_weight is a list of 3 tensors, or None
    if not None:
        class_weight[0], len 168, weight of root
        class_weight[1], len 11, weight of vowel
        class_weight[2], len 7, weight of consonant
    """
    # if mixed_precision:
    #     from apex import amp
    since = time.time()

    export_logger = ExportLogger(save_dir)

    # need to co-change ExportLogger.update_from_callbackrecorder if want to
    # change list_of_field
    # prefer dont change
    list_of_field = [
        "train_loss",
        "train_root_acc",
        "train_vowel_acc",
        "train_consonant_acc",
        "train_combined_acc",
        "train_root_recall",
        "train_vowel_recall",
        "train_consonant_recall",
        "train_combined_recall",
        "val_loss",
        "val_root_acc",
        "val_vowel_acc",
        "val_consonant_acc",
        "val_combined_acc",
        "val_root_recall",
        "val_vowel_recall",
        "val_consonant_recall",
        "val_combined_recall",
        "no_aug_train_loss",
        "no_aug_train_root_acc",
        "no_aug_train_vowel_acc",
        "no_aug_train_consonant_acc",
        "no_aug_train_combined_acc",
        "no_aug_train_root_recall",
        "no_aug_train_vowel_recall",
        "no_aug_train_consonant_recall",
        "no_aug_train_combined_recall",
    ]
    export_logger.define_field_to_record(list_of_field)
    
    for epoch in range(num_epochs):
        print("Epoch {}/{}".format(epoch, num_epochs - 1))
        print("-" * 10)

        # SWA
        start_swa = False
        if swa:
            if num_epochs - epoch < 30:  # Start averaging at last 25% ep
                start_swa = True

        train_recorder = train_phrase(
            model=model,
            optimizer=optimizer,
            train_dataloader=dataloaders["train"],
            mixed_precision=mixed_precision,
            train_loss_prob_2=train_loss_prob_2,
            extra_augmentation_prob=extra_augmentation_prob,
            head_weights=head_weights,
            class_weights=class_weights,
            ohem_rate=ohem_rate,
            mixup_alpha=mixup_alpha,
            cutmix_alpha=cutmix_alpha,
            batch_scheduler=batch_scheduler,
            wandb_log=wandb_log,
            start_swa=start_swa,
        )

        if start_swa:
            print("CycleLR snapshot")  # Update once per ep
            optimizer.update_swa()
            if epoch == (num_epochs - 1):  # Merge at end of last ep
                optimizer.swap_swa_sgd()
                optimizer.bn_update(dataloaders["train"], model)  # Update batch stat
                print("SWA Merge Models")

        print("Finish training")
        train_recorder.print_statistics()
        print()

        valid_recorder = validate_phrase(model, dataloaders["val"], wandb_log=wandb_log)
        print("Finish validating")
        valid_recorder.print_statistics()
        print()

        no_aug_recorder = validate_phrase(
            model, dataloaders["no_aug"], wandb_log=wandb_log, phrase="no aug"
        )
        print("Finish no aug validation")
        no_aug_recorder.print_statistics()
        print()

        # update lr scheduler
        val_loss = valid_recorder.get_loss()
        if error_plateau_scheduler:
            error_plateau_scheduler.step(val_loss)

        # record training statistics into ExportLogger
        export_logger.update_from_callbackrecorder(
            train_recorder, valid_recorder, no_aug_recorder
        )

        # check whether val_loss gets lower/val_combined_recall gets higher
        # also save the model.pth is required
        export_logger.export_models_and_csv(model, valid_recorder)

        print()

    time_elapsed = time.time() - since
    print(
        "Training complete in {:.0f}m {:.0f}s".format(
            time_elapsed // 60, time_elapsed % 60
        )
    )
    return export_logger.callbacks
