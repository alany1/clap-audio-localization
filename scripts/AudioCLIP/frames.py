import os

import numpy as np
import torch
import torchvision
from PIL import Image, ImageOps
from params_proto import Proto, ParamsProto, PrefixProto
from torchvision import io
from torchvision.transforms import ToTensor
from tqdm import tqdm

from scripts.AudioCLIP.model import AudioCLIP
import time
import shutil
from matplotlib import pyplot as plt

SCALE_AUDIO_IMAGE = 68.1320
SCALE_IMAGE_TEXT = 81.9903

def MAP(similarity):
    """
    Scaling function that takes in similarity values and maps them to 0-100 range with
    more emphasis on higher values and less on lower values with a sigmoid-like curve.

    The std affects how slow the image gradient dies out.
    The topX affects how much of the pixels actually matter.
    """
    std = similarity.std()
    top25 = torch.topk(similarity[:, 0], k=int(len(similarity) * 0.05)).values.min()
    return 100 / (1 + torch.exp(-(similarity - top25) / (std / 12)))


class FrameArgs(PrefixProto):
    IMAGE_SIZE = 224  # derived from CLIP, how to upscale the patches basically
    IMAGE_MEAN = 0.48145466, 0.4578275, 0.40821073
    IMAGE_STD = 0.26862954, 0.26130258, 0.27577711

    device = "cuda"
    video_path = "../examples/beach.mov"
    patch_size = 128
    downscale = 32

def extract_patches_rect(image, patch_size, patches_per_row, patches_per_column):
    patches = []
    width, height = image.size

    padding_x = (patch_size - width % patch_size) % patch_size
    padding_y = (patch_size - height % patch_size) % patch_size
    padded_image = ImageOps.expand(
        image,
        (
            padding_x // 2,
            padding_y // 2,
            padding_x - padding_x // 2,
            padding_y - padding_y // 2,
        ),
    )

    padded_width, padded_height = padded_image.size
    stride_x = (padded_width - patch_size) // (patches_per_row - 1)
    stride_y = (padded_height - patch_size) // (patches_per_column - 1)

    for y in range(0, padded_height - patch_size + 1, stride_y):
        for x in range(0, padded_width - patch_size + 1, stride_x):
            patch = ToTensor()(
                padded_image.crop((x, y, x + patch_size, y + patch_size))
            )
            patches.append(patch)

    patches = torch.stack(patches, dim=0)
    return patches, stride_x, stride_y, padded_width, padded_height


def visualize_embeddings(embedding):
    """
    Visualize the first three principal components of embedding to correspond to RGB.
    """
    import matplotlib.pyplot as plt
    import torch
    from sklearn.decomposition import PCA

    # Reshape the embedding into (N, embedding_dim)
    h, w = embedding.shape[:-1]
    embedding = embedding.reshape(-1, embedding.shape[-1])

    # Apply PCA to reduce the dimensionality of the embedding
    pca = PCA(n_components=3)
    pca.fit(embedding.detach().cpu().numpy())
    embedding = torch.from_numpy(pca.transform(embedding.detach().cpu().numpy()))

    # Reshape the embedding back into (N, 24, 24, 3)
    embedding = embedding.reshape(-1, w, h, 3)
    return embedding


def get_frame_embeddings(model):
    """
    Currently only supports a single frame
    TODO: play with batch size.
    """
    image_transforms = torchvision.transforms.Compose(
        [
            torchvision.transforms.Resize(
                FrameArgs.IMAGE_SIZE, interpolation=Image.BICUBIC, antialias=True
            ),
            torchvision.transforms.CenterCrop(FrameArgs.IMAGE_SIZE),
            torchvision.transforms.Normalize(FrameArgs.IMAGE_MEAN, FrameArgs.IMAGE_STD),
        ]
    )
    model.to(FrameArgs.device)
    if FrameArgs.video_path.split(".")[-1] == "jpeg":
        print("Processing the image", FrameArgs.video_path)
        image = Image.open(FrameArgs.video_path)
        images = [image]
    else:
        print("Processing the video", FrameArgs.video_path)
        video_reader = io.read_video(FrameArgs.video_path, pts_unit="sec")
        video_tensor, audio_tensor, video_info = video_reader
        images = [Image.fromarray(frame.numpy()) for frame in video_tensor[-1:]]

    w, h = images[0].size

    t0 = time.time()
    patches, stride_x, stride_y, padded_w, padded_h = extract_patches_rect(
        images[0],
        FrameArgs.patch_size,
        w // FrameArgs.downscale,
        h // FrameArgs.downscale,
    )
    print(f"Extracting patches took {time.time() - t0} seconds")

    all_patches = torch.stack([image_transforms(patch) for patch in patches])

    image_features = []
    for i in tqdm(range(0, all_patches.shape[0], 8), desc="Extracting image features"):
        image_features.append(model(image=all_patches[i : i + 8].to(FrameArgs.device)))
        # move back to CPU to reduce GPU memory usage
        image_features[-1] = image_features[-1][0][0][1].detach().cpu()
    image_features = torch.cat(image_features, dim=0)

    image_features = image_features / torch.linalg.norm(
        image_features, dim=-1, keepdim=True
    )

    new_w = (padded_w - FrameArgs.patch_size) // stride_x + 1
    new_h = (padded_h - FrameArgs.patch_size) // stride_y + 1

    return image_features, new_w, new_h, images


def save_frame_embeddings(
    model, num_frames=1, tmp_dir="/tmp/frames", skip=True, scale=1
):
    """
    To circumvent memory issues, we write the features to disk in a temporary directory for later use.

    We return the path to the temporary directory along with (new_h, new_w, images).
    """
    model.to(FrameArgs.device)
    image_transforms = torchvision.transforms.Compose(
        [
            torchvision.transforms.Resize(
                FrameArgs.IMAGE_SIZE, interpolation=Image.BICUBIC, antialias=True
            ),
            torchvision.transforms.CenterCrop(FrameArgs.IMAGE_SIZE),
            torchvision.transforms.Normalize(FrameArgs.IMAGE_MEAN, FrameArgs.IMAGE_STD),
        ]
    )
    print("Reading video")
    video_reader = io.read_video(FrameArgs.video_path, pts_unit="sec")
    video_tensor, audio_tensor, video_info = video_reader
    print("total frames in video", video_tensor.shape[0])
    # Resize these images to be smaller 2x on each axis
    im0 = Image.fromarray(video_tensor[0].numpy())
    w, h = im0.size

    print("Resizing images to be ", w // scale, "by", h // scale)
    images = [
        Image.fromarray(frame.numpy()).resize((w // scale, h // scale))
        for frame in video_tensor
    ]
    w, h = images[0].size

    print("extracting one to get the stride")
    patches, stride_x, stride_y, padded_w, padded_h = extract_patches_rect(
        images[0],
        FrameArgs.patch_size,
        w // FrameArgs.downscale,
        h // FrameArgs.downscale,
    )

    if not skip:
        proceed = input("This will overwrite the temporary directory. Proceed? (y/n)")
        if proceed != "y":
            raise Exception("Aborting")
        try:
            print("Removing old temporary directory")
            shutil.rmtree(tmp_dir)
        except FileNotFoundError:
            print("No old temporary directory found, proceeding...")

        print("Creating new temporary directory")
        os.mkdir(tmp_dir)

        # time this line
        for x in tqdm(
            range(num_frames),
            desc="Extracting image features for each frame 🎥📸",
            colour="green",
        ):
            t0 = time.time()
            patches, stride_x, stride_y, padded_w, padded_h = extract_patches_rect(
                images[x],
                FrameArgs.patch_size,
                w // FrameArgs.downscale,
                h // FrameArgs.downscale,
            )
            if x % 10 == 0:
                print(
                    f"Extracting patches for frame {x+1} took {time.time() - t0} seconds"
                )

            all_patches = torch.stack([image_transforms(patch) for patch in patches])

            image_features = []
            for i in range(0, all_patches.shape[0], 8):
                image_features.append(
                    model(image=all_patches[i : i + 8].to(FrameArgs.device))
                )
                # move back to CPU to reduce GPU memory usage
                image_features[-1] = image_features[-1][0][0][1].detach().cpu()
            image_features = torch.cat(image_features, dim=0)

            image_features = image_features / torch.linalg.norm(
                image_features, dim=-1, keepdim=True
            )

            # save the features into the temporary directory
            torch.save(image_features, os.path.join(tmp_dir, f"frame_{x}.pt"))
    else:
        print("Skipping feature extraction, loading from disk")
    new_w = (padded_w - FrameArgs.patch_size) // stride_x + 1
    new_h = (padded_h - FrameArgs.patch_size) // stride_y + 1

    return tmp_dir, new_w, new_h, images


def negative_sample(source_feature, embedding, k=1, threshold=0.1, disable_pbar=True):
    """
    Given a source feature, we sample k negative examples from the embedding.

    embedding: [num_patches, embedding_dim]
    Return: negative_sample; size of [k, embedding_dim]
    """

    negatives = []
    for _ in tqdm(
        range(k), desc=f"Sampling {k} negative examples", disable=disable_pbar
    ):
        # Sample a random patch
        while True:
            idx = np.random.randint(embedding.shape[0])
            negative = embedding[idx]

            # Reject if the patch is too similar to the source feature
            if negative @ source_feature.T >= threshold:
                continue

            negatives.append(negative)
            break
    return torch.stack(negatives)


def process_frames(
    tmp_dir,
    source_feature,
    new_w,
    new_h,
    images,
    num_frames=100,
    supervision_feature=None,
    lambda_=0.2,
):
    """
    Given the temporary directory, we load the features and generate a series of heatmaps showing similarity
    with the source_feature.

    If supervision_feature is included, we compute the resultant heatmap as the interaction between source
    and supervision heatmaps.
    """
    movie = []  # list of cmap heatmaps representing the output video

    if supervision_feature is not None:
        print("Processing frames with supervision feature")

    for x in tqdm(
        range(min(len(images), num_frames)),
        desc="Processing frames individually 🎥📸",
        colour="green",
    ):
        embedding = torch.load(os.path.join(tmp_dir, f"frame_{x}.pt")).to(
            FrameArgs.device
        )

        # Compute the similarity between the source feature and the current frame
        unscaled_similarity = embedding @ source_feature.T
        similarity = SCALE_AUDIO_IMAGE * unscaled_similarity

        if supervision_feature is not None:
            # Compute the similarity between the supervision feature and the current frame
            supervision_similarity = torch.mean(
                SCALE_IMAGE_TEXT * embedding @ supervision_feature.T,
                dim=-1,
                keepdim=True,
            )
            # Compute the interaction between the two
            similarity = (similarity + supervision_similarity) / 2

        # Negative sampling (random): select a random patch, subtract lambda*similarity to that negative sample.
        # Threshold will be the lower 25% of the similarity values.
        threshold = torch.quantile(unscaled_similarity, 0.7)
        negatives = negative_sample(
            source_feature, embedding, k=40, threshold=threshold
        )

        # Penalty is lambda * avg similarity to the negative samples
        penalty = SCALE_AUDIO_IMAGE * (
            torch.mean(embedding @ negatives.T, dim=-1)
        )

        similarity = similarity - lambda_ * penalty.unsqueeze(-1)

        # Map the similarity using a function that favors higher values and keeps things near the mean low
        similarity = MAP(similarity)
        similarity = similarity.reshape(new_h, new_w)

        # scaling and video creation is done by the caller
        movie.append(similarity.cpu())

    return movie


if __name__ == "__main__":
    from matplotlib import pyplot as plt

    MODEL_FILENAME = "AudioCLIP-Full-Training.pt"
    FrameArgs.video_path = "../examples/beach.mov"
    aclp = AudioCLIP(pretrained=f"assets/{MODEL_FILENAME}")
    aclp.eval()

    # embeddings, new_w, new_h, images = get_frame_embeddings(aclp)
    tmp_dir, new_w, new_h, images = save_frame_embeddings(aclp, num_frames=100)

    # pre_viz = embeddings.reshape(new_w, new_h, -1)
    # viz = visualize_embeddings(pre_viz)
    # #
    # # # normalize
    # viz = (viz - viz.min()) / (viz.max() - viz.min())
    #
    # fig, axs = plt.subplots(1,2)
    # axs[0].imshow(viz[0])
    # axs[0].set_title("PCA Visualization")
    # axs[1].imshow(images[0])
    # axs[1].set_title("Original Frame")
    # plt.show()
