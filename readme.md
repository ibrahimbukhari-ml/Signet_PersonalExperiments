# Improving Sign Language Backbones for Sign Language Translation task

The goal of these experiments is to analyse and judge different variations of sign language translation backbones in order to analyse the tradeoff for each configuration. Furthermore, these experiments will allow us to determine which components within the backbone actually helps us to improve the underlying sign language data understanding and in doing so will also help us determine which component reacts best when scaled. 

There are multiple components within the backbones, which are listed below.

## Baseline Backbone:

**Video Datasets**  -->  **RTMPose Keypoint extraction**  -->  **ST-GCN Keypoint processing**  -->  **ST-GCN feature vectors (FVs)**

**ST-GCN FVs to LLM projection**  -->  **LLM training on target translation**


## RGB Backbone:

**Video Datasets**  -->  **RGB Frames extraction**  -->  **RGB Frame processing**  -->  **RGB frame FVs**

**RGB Frame FVs to LLM projection**  -->  **LLM training on target translation**


## RGB + Pose Backbone:

**(ST-GCN FVs + RGB Frame FVs)**  -->  **Fusion FVs to LLM projection**  -->  **LLM training on target translation** 

