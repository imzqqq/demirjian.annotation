# 用于加载模型进行预测
import colorsys
import os

import numpy as np
import torch
import torch.nn as nn
from PIL import Image, ImageDraw, ImageFont

from nets.yolo3 import YoloBody
from nets.utils import (DecodeBox, letterbox_image, non_max_suppression, yolo_correct_boxes)


class YOLO(object):
    # 模型设置
    _defaults = {
        "model_path": 'model_data/yolo_tooth_weight.pth',
        "anchors_path": 'model_data/yolo_anchors.txt',
        "classes_path": 'model_data/tooth_classes.txt',
        "model_image_size": (192, 416, 3),  # h, w
        "confidence": 0.5,
        "iou": 0.3,
        "cuda": True,
        "letterbox_image": False,
    }

    @classmethod
    def get_defaults(cls, n):
        if n in cls._defaults:
            return cls._defaults[n]
        else:
            return "Unrecognized attribute name '" + n + "'"

    # 初始化
    def __init__(self, **kwargs):
        self.__dict__.update(self._defaults)
        self.class_names = self._get_class()
        self.anchors = self._get_anchors()
        self.num_classes = len(self.class_names)
        self.generate()

    # 获取分类
    def _get_class(self):
        classes_path = os.path.expanduser(self.classes_path)
        with open(classes_path) as f:
            class_names = f.readlines()
        class_names = [c.strip() for c in class_names]
        return class_names

    # 获取所有的先验框
    def _get_anchors(self):
        anchors_path = os.path.expanduser(self.anchors_path)
        with open(anchors_path) as f:
            anchors = f.readline()
        anchors = [float(x) for x in anchors.split(',')]
        return np.array(anchors).reshape([-1, 3, 2])[::-1, :, :]

    # 生成模型
    def generate(self):
        self.num_classes = len(self.class_names)

        # 建立yolov3网络模型   对应net变量
        self.net = YoloBody(self.anchors, self.num_classes)

        # 载入训练好的权重
        print('Loading weights into state dict...')
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        state_dict = torch.load(self.model_path, map_location=device)
        self.net.load_state_dict(state_dict)
        self.net = self.net.eval()

        if self.cuda:
            self.net = nn.DataParallel(self.net)
            self.net = self.net.cuda()

        self.yolo_decodes = []
        for i in range(3):
            self.yolo_decodes.append(
                DecodeBox(self.anchors[i], self.num_classes, (self.model_image_size[1], self.model_image_size[0])))

        print('{} model, anchors, and classes loaded.'.format(self.model_path))
        # 画框设置不同的颜色
        hsv_tuples = [(x / len(self.class_names), 1., 1.)
                      for x in range(len(self.class_names))]
        self.colors = list(map(lambda x: colorsys.hsv_to_rgb(*x), hsv_tuples))
        self.colors = list(
            map(lambda x: (int(x[0] * 255), int(x[1] * 255), int(x[2] * 255)),
                self.colors))

    def detect_image(self, image, name):
        # 叠加成3通道的
        location_list = []
        # image = image.convert('RGB')

        image_shape = np.array(np.shape(image)[0:2])
        # print("image_shape", np.shape(image))

        # resize
        if self.letterbox_image:
            crop_img = np.array(letterbox_image(image, (self.model_image_size[1], self.model_image_size[0])))
        else:
            crop_img = image.resize((self.model_image_size[1], self.model_image_size[0]), Image.BICUBIC)

        photo = np.array(crop_img, dtype=np.float32) / 255.0
        photo = np.transpose(photo, (2, 0, 1))

        images = [photo]

        with torch.no_grad():
            images = torch.from_numpy(np.asarray(images))
            if self.cuda:
                images = images.cuda()

            # 将图像输入到网络中进行预测
            outputs = self.net(images)
            output_list = []
            for i in range(3):
                output_list.append(self.yolo_decodes[i](outputs[i]))

            # 将预测框进行堆叠，然后进行非极大抑制
            output = torch.cat(output_list, 1)
            batch_detections = non_max_suppression(output, self.num_classes, conf_thres=self.confidence,
                                                   nms_thres=self.iou)

            # print('batch_detections', batch_detections)
            # print('batch_detections_shape', batch_detections[0].size())

            try:
                batch_detections = batch_detections[0].cpu().numpy()
            except:
                return image

            top_index = batch_detections[:, 4] * batch_detections[:, 5] > self.confidence   # 根据置信度进行筛选
            # 输出包含true false的列表
            # print('top_index', top_index)
            top_conf = batch_detections[top_index, 4] * batch_detections[top_index, 5]
            top_label = np.array(batch_detections[top_index, -1], np.int32)
            top_bboxes = np.array(batch_detections[top_index, :4])
            top_xmin, top_ymin, top_xmax, top_ymax = np.expand_dims(top_bboxes[:, 0], -1), np.expand_dims(
                top_bboxes[:, 1], -1), np.expand_dims(top_bboxes[:, 2], -1), np.expand_dims(top_bboxes[:, 3], -1)

            if self.letterbox_image:
                boxes = yolo_correct_boxes(top_ymin, top_xmin, top_ymax, top_xmax,
                                           np.array([self.model_image_size[0], self.model_image_size[1]]), image_shape)
            else:
                top_xmin = top_xmin / self.model_image_size[1] * image_shape[1]
                top_ymin = top_ymin / self.model_image_size[0] * image_shape[0]
                top_xmax = top_xmax / self.model_image_size[1] * image_shape[1]
                top_ymax = top_ymax / self.model_image_size[0] * image_shape[0]
                boxes = np.concatenate([top_ymin, top_xmin, top_ymax, top_xmax], axis=-1)

        for i, c in enumerate(top_label):
            predicted_class = self.class_names[c]
            score = top_conf[i]

            top, left, bottom, right = boxes[i]
            # print('loc:', top, left, bottom, right)

            # top = max(0, np.floor(top + 0.5).astype('int32'))
            # left = max(0, np.floor(left + 0.5).astype('int32'))
            # bottom = min(np.shape(image)[0], np.floor(bottom + 0.5).astype('int32'))   # 不要超出边界
            # right = min(np.shape(image)[1], np.floor(right + 0.5).astype('int32'))

            label = '{} {:.2f}'.format(predicted_class, score)
            # draw = ImageDraw.Draw(image)
            # label_size = draw.textsize(label, font)
            # label = label.encode('utf-8')
            location_box = [top, left, bottom, right]
            location_box = np.array(location_box).tolist()
            location_list.append(location_box)
            # print(label, top, left, bottom, right)

        return location_list
