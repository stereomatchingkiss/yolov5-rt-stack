# Copyright (c) 2021, Zhiqiang Wang. All Rights Reserved.
import warnings
import argparse
from pathlib import PosixPath

import torch
from torch import Tensor

from pytorch_lightning import LightningModule

from . import yolo
from .transform import GeneralizedYOLOTransform
from ._utils import _evaluate_iou
from ..data import DetectionDataModule, DataPipeline, COCOEvaluator

from typing import Any, List, Dict, Tuple, Optional, Union

__all__ = ['YOLOModule']


class YOLOModule(LightningModule):
    """
    PyTorch Lightning wrapper of `YOLO`
    """
    def __init__(
        self,
        lr: float = 0.01,
        arch: str = 'yolov5_darknet_pan_s_r31',
        pretrained: bool = False,
        progress: bool = True,
        num_classes: int = 80,
        min_size: int = 320,
        max_size: int = 416,
        annotation_path: Optional[Union[str, PosixPath]] = None,
        **kwargs: Any,
    ):
        """
        Args:
            arch: architecture
            lr: the learning rate
            pretrained: if true, returns a model pre-trained on COCO train2017
            num_classes: number of detection classes (doesn't including background)
        """
        super().__init__()

        self.lr = lr
        self.num_classes = num_classes

        self.model = yolo.__dict__[arch](
            pretrained=pretrained, progress=progress, num_classes=num_classes, **kwargs)

        self.transform = GeneralizedYOLOTransform(min_size, max_size)

        self._data_pipeline = None

        # metrics
        self.evaluator = None
        if annotation_path is not None:
            self.evaluator = COCOEvaluator(annotation_path, iou_type="bbox")

        # used only on torchscript mode
        self._has_warned = False

    def _forward_impl(
        self,
        inputs: List[Tensor],
        targets: Optional[List[Dict[str, Tensor]]] = None,
    ) -> List[Dict[str, Tensor]]:
        """
        Args:
            inputs (list[Tensor]): images to be processed
            targets (list[Dict[Tensor]]): ground-truth boxes present in the image (optional)

        Returns:
            result (list[BoxList] or dict[Tensor]): the output from the model.
                During training, it returns a dict[Tensor] which contains the losses.
                During testing, it returns list[BoxList] contains additional fields
                like `scores`, `labels` and `mask` (for Mask R-CNN models).
        """
        # get the original image sizes
        original_image_sizes: List[Tuple[int, int]] = []

        if not self.training:
            for img in inputs:
                val = img.shape[-2:]
                assert len(val) == 2
                original_image_sizes.append((val[0], val[1]))

        # Transform the input
        samples, targets = self.transform(inputs, targets)
        # Compute the detections
        outputs = self.model(samples.tensors, targets=targets)

        losses = {}
        detections: List[Dict[str, Tensor]] = []

        if self.training:
            # compute the losses
            if torch.jit.is_scripting():
                losses = outputs[0]
            else:
                losses = outputs
        else:
            # Rescale coordinate
            detections = self.transform.postprocess(outputs, samples.image_sizes, original_image_sizes)

        if torch.jit.is_scripting():
            if not self._has_warned:
                warnings.warn("YOLOModule always returns Detections in scripting.")
                self._has_warned = True
            return detections
        else:
            return self.eager_outputs(losses, detections)

    @torch.jit.unused
    def eager_outputs(
        self,
        losses: Dict[str, Tensor],
        detections: List[Dict[str, Tensor]],
    ) -> Tuple[Dict[str, Tensor], List[Dict[str, Tensor]]]:
        if self.training:
            return losses

        return detections

    def forward(
        self,
        inputs: List[Tensor],
        targets: Optional[List[Dict[str, Tensor]]] = None,
    ) -> List[Dict[str, Tensor]]:
        """
        This exists since PyTorchLightning forward are used for inference only (separate from
        ``training_step``). We keep ``targets`` here for Backward Compatible.
        """
        return self._forward_impl(inputs, targets)

    def training_step(self, batch, batch_idx):
        """
        The training step.
        """
        loss_dict = self._forward_impl(*batch)
        loss = sum(loss_dict.values())
        self.log_dict(loss_dict, on_step=True, on_epoch=True, prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        images, targets = batch
        # fasterrcnn takes only images for eval() mode
        preds = self._forward_impl(images)
        iou = torch.stack([_evaluate_iou(t, o) for t, o in zip(targets, preds)]).mean()
        outs = {"val_iou": iou}
        self.log_dict(outs, on_step=True, on_epoch=True, prog_bar=True)
        return outs

    def validation_epoch_end(self, outs):
        avg_iou = torch.stack([o["val_iou"] for o in outs]).mean()
        self.log("avg_val_iou", avg_iou)

    def test_step(self, batch, batch_idx):
        """
        The test step.
        """
        images, targets = batch
        images = list(image.to(self.device) for image in images)
        preds = self._forward_impl(images)
        results = self.evaluator(preds, targets)
        # log step metric
        self.log('eval_step', results, prog_bar=True, on_step=True)

    def test_epoch_end(self, outputs):
        return self.log('coco_eval', self.evaluator.compute())

    @torch.no_grad()
    def predict(
        self,
        x: Any,
        batch_idx: Optional[int] = None,
        skip_collate_fn: bool = False,
        dataloader_idx: Optional[int] = None,
        data_pipeline: Optional[DataPipeline] = None,
    ) -> Any:
        """
        Predict function for raw data or processed data

        Args:

            x: Input to predict. Can be raw data or processed data.

            batch_idx: Batch index

            dataloader_idx: Dataloader index

            skip_collate_fn: Whether to skip the collate step.
                this is required when passing data already processed
                for the model, for example, data from a dataloader

            data_pipeline: Use this to override the current data pipeline

        Returns:
            The post-processed model predictions

        """
        data_pipeline = data_pipeline or self.data_pipeline
        batch = x if skip_collate_fn else data_pipeline.collate_fn(x)
        images, _ = batch if len(batch) == 2 and isinstance(batch, (list, tuple)) else (batch, None)
        images = [img.to(self.device) for img in images]
        predictions = self.forward(images)
        output = data_pipeline.uncollate_fn(predictions)  # TODO: pass batch and x
        return output

    def configure_optimizers(self):
        return torch.optim.SGD(
            self.model.parameters(),
            lr=self.lr,
            momentum=0.9,
            weight_decay=0.005,
        )

    @torch.jit.unused
    @property
    def data_pipeline(self) -> DataPipeline:
        # we need to save the pipeline in case this class
        # is loaded from checkpoint and used to predict
        if not self._data_pipeline:
            self._data_pipeline = self.default_pipeline()
        return self._data_pipeline

    @data_pipeline.setter
    def data_pipeline(self, data_pipeline: DataPipeline) -> None:
        self._data_pipeline = data_pipeline

    @staticmethod
    def default_pipeline() -> DataPipeline:
        """Pipeline to use when there is no datamodule or it has not defined its pipeline"""
        return DetectionDataModule.default_pipeline()

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = argparse.ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument('--arch', default='yolov5_darknet_pan_s_r31',
                            help='model architecture')
        parser.add_argument('--num_classes', default=80, type=int,
                            help='number classes of datasets')
        parser.add_argument('--pretrained', action='store_true',
                            help='Use pre-trained models from the modelzoo')
        parser.add_argument('--lr', default=0.02, type=float,
                            help='initial learning rate, 0.02 is the default value for training '
                            'on 8 gpus and 2 images_per_gpu')
        parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                            help='momentum')
        parser.add_argument('--weight-decay', default=5e-4, type=float,
                            metavar='W', help='weight decay (default: 5e-4)')
        return parser
