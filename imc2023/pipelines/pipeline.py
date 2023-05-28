"""Abstract pipeline class."""
import argparse
import logging
import shutil
import subprocess
import time
from abc import abstractmethod
from typing import Any, Dict, List

import cv2
import h5py
import numpy as np
from tqdm import tqdm
import pycolmap
from hloc import extract_features, match_features, pairs_from_exhaustive, pairs_from_retrieval, reconstruction
from hloc.utils.io import list_h5_names, get_matches, get_keypoints

from imc2023.preprocessing import preprocess_image_dir
from imc2023.utils.concatenate import concat_features, concat_matches
from imc2023.utils.utils import DataPaths
from imc2023.utils import rot_mat_z, rotmat2qvec

def time_function(func):
    """Time a function."""

    def wrapper(*args, **kwargs):
        start = time.time()
        func(*args, **kwargs)
        return time.time() - start

    return wrapper


class Pipeline:
    """Abstract pipeline class."""

    def __init__(
        self,
        config: Dict[str, Any],
        paths: DataPaths,
        img_list: List[str],
        args: argparse.Namespace,
    ) -> None:
        """Initialize the pipeline.

        Args:
            config (Dict[str, Any]): Configuration dictionary.
            paths (DataPaths): Data paths.
            img_list (List[str]): List of image names.
            # use_pixsfm (bool, optional): Whether to use PixSFM. Defaults to False.
            # pixsfm_max_imgs (int, optional): Max number of images for PixSFM. Defaults to 9999.
            # pixsfm_config (str, optional): Which PixSFM config to use. Defaults to low_memory.
            # pixsfm_script_path (str, optional): Path to run_pixsfm.py. Needs to be changed for Euler.
            # use_rotation_matching (bool, optional): Whether to use rotation matching. Defaults to False.
            overwrite (bool, optional): Whether to overwrite previous output files. Defaults to False.
        """
        self.config = config
        self.paths = paths
        self.img_list = img_list
        self.use_pixsfm = args.pixsfm
        self.pixsfm_max_imgs = args.pixsfm_max_imgs
        self.pixsfm_config = args.pixsfm_config
        self.pixsfm_script_path = args.pixsfm_script_path
        self.use_rotation_matching = args.rotation_matching
        self.use_rotation_wrapper = args.rotation_wrapper
        self.use_cropping = args.cropping
        self.max_rel_crop_size = args.max_rel_crop_size
        self.min_rel_crop_size = args.min_rel_crop_size
        self.overwrite = args.overwrite
        self.args = args

        self.sparse_model = None
        self.rotated_sparse_model = None

        self.is_ensemble = type(self.config["features"]) == list
        if self.is_ensemble:
            assert len(self.config["features"]) == len(
                self.config["matches"]
            ), "Number of features and matches must be equal for ensemble matching."
            assert (
                len(self.config["features"]) == 2
            ), "Only two features are supported for ensemble matching."

        self.rotation_angles = {}

        self.timing = {
            "preprocess": 0,
            "get_pairs": 0,
            "extract_features": 0,
            "match_features": 0,
            "create_ensemble": 0,
            "rotate_keypoints": 0,
            "sfm": 0,
            "localize_unregistered": 0,
            "back-rotate-cameras":0
        }

    def log_step(self, title: str) -> None:
        """Log a title.

        Args:
            title: The title to log.
        """
        logging.info(f"{'=' * 80}")
        logging.info(title)
        logging.info(f"{'=' * 80}")

    def preprocess(self) -> None:
        """Preprocess the images."""
        self.log_step("Preprocessing")
        self.rotation_angles = preprocess_image_dir(
            input_dir=self.paths.input_dir,
            output_dir=self.paths.scene_dir,
            image_list=self.img_list,
            args=self.args,
        )

    def get_pairs(self) -> None:
        """Get pairs of images to match."""
        self.log_step("Get pairs")

        if len(self.img_list) < self.config["n_retrieval"]:
            pairs_from_exhaustive.main(output=self.paths.pairs_path, image_list=self.img_list)
            return

        if self.paths.pairs_path.exists() and not self.overwrite:
            logging.info(f"Pairs already at {self.paths.pairs_path}")
            return
        else:
            if self.use_rotation_matching or self.use_rotation_wrapper:
                image_dir = self.paths.rotated_image_dir
            else:
                image_dir = self.paths.image_dir

            extract_features.main(
                conf=self.config["retrieval"],
                image_dir=image_dir,
                image_list=self.img_list,
                feature_path=self.paths.features_retrieval,
            )

        pairs_from_retrieval.main(
            descriptors=self.paths.features_retrieval,
            num_matched=self.config["n_retrieval"],
            output=self.paths.pairs_path,
        )

    @abstractmethod
    def extract_features(self) -> None:
        """Extract features from the images."""
        pass

    @abstractmethod
    def match_features(self) -> None:
        """Match features between images."""
        pass

    def create_ensemble(self) -> None:
        """Concatenate features and matches."""
        if not self.is_ensemble:
            return

        self.log_step("Creating ensemble")

        feature_path = self.paths.features_path
        if self.use_rotation_matching:
            feature_path = self.paths.rotated_features_path

        fpath1 = self.paths.features_path.parent / f'{self.config["features"][0]["output"]}.h5'
        fpath2 = self.paths.features_path.parent / f'{self.config["features"][1]["output"]}.h5'

        concat_features(
            features1=fpath1,
            features2=fpath2,
            out_path=feature_path,
        )

        mpath1 = self.paths.matches_path.parent / f'{self.config["matches"][0]["output"]}.h5'
        mpath2 = self.paths.matches_path.parent / f'{self.config["matches"][1]["output"]}.h5'

        concat_matches(
            matches1_path=mpath1,
            matches2_path=mpath2,
            ensemble_features_path=feature_path,
            out_path=self.paths.matches_path,
        )

        pairs = sorted(list(list_h5_names(self.paths.matches_path)))

        with open(self.paths.pairs_path, "w") as f:
            for pair in pairs:
                p = pair.split("/")
                f.write(f"{p[0]} {p[1]}\n")
    
    def perform_cropping(self):
        """Crop images for each pair and use them to add additional matches."""
        if not self.use_cropping:
            return
        
        self.log_step("Performing image cropping")

        logging.info("Creating crops for all matches")

        # new list of pairs for the matching on crops
        crop_pairs = []

        # dictionary of offsets to transform the keypoints from "crop spaces" to the original image spaces
        offsets = {}

        # iterate through all original pairs and create crops
        original_pairs = list(list_h5_names(self.paths.matches_path))
        for pair in tqdm(original_pairs):
            img_1, img_2 = pair.split("/")

            # get original keypoints and matches
            kp_1 = get_keypoints(self.paths.features_path, img_1).astype(np.int32)
            kp_2 = get_keypoints(self.paths.features_path, img_2).astype(np.int32)
            matches, scores = get_matches(self.paths.matches_path, img_1, img_2)

            if len(matches) < 100:
                continue # too few matches

            # get top 80% matches
            threshold = np.quantile(scores, 0.2)
            mask = scores >= threshold
            top_matches = matches[mask]

            # compute bounding boxes based on the keypoints of the top 80% matches
            top_kp_1 = kp_1[top_matches[:,0]]
            top_kp_2 = kp_2[top_matches[:,1]]
            original_image_1 = cv2.imread(str(self.paths.image_dir / img_1))
            original_image_2 = cv2.imread(str(self.paths.image_dir / img_2))
            cropped_image_1 = original_image_1[
                top_kp_1[:, 1].min() : top_kp_1[:, 1].max() + 1, 
                top_kp_1[:, 0].min() : top_kp_1[:, 0].max() + 1, 
            ]
            cropped_image_2 = original_image_2[
                top_kp_2[:, 1].min() : top_kp_2[:, 1].max() + 1, 
                top_kp_2[:, 0].min() : top_kp_2[:, 0].max() + 1, 
            ]

            # check if the relative size conditions are fulfilled
            rel_size_1 = cropped_image_1.size / original_image_1.size
            rel_size_2 = cropped_image_2.size / original_image_2.size

            if rel_size_1 <= self.min_rel_crop_size or rel_size_2 < self.min_rel_crop_size:
                # one of the crops or both crops are too small ==> avoid degenerate crops
                continue 

            if rel_size_1 >= self.max_rel_crop_size and rel_size_2 >= self.max_rel_crop_size:
                # both crops are almost the same size as the original images
                # ==> crops are not useful (almost same matches as on the original images)
                continue

            # define new names for the crops based on the current pair because each 
            # original image will be cropped in a different way for each original match
            name_1 = f"{img_1[:-4]}_{img_2[:-4]}_1.jpg"
            name_2 = f"{img_1[:-4]}_{img_2[:-4]}_2.jpg"

            # save crops
            cv2.imwrite(str(self.paths.cropped_image_dir / name_1), cropped_image_1)
            cv2.imwrite(str(self.paths.cropped_image_dir / name_2), cropped_image_2)

            # create new matching pair and save offsets for image space transformations
            crop_pairs.append((name_1, name_2))
            offsets[name_1] = (top_kp_1[:, 0].min(), top_kp_1[:, 1].min())
            offsets[name_2] = (top_kp_2[:, 0].min(), top_kp_2[:, 1].min())
        
        # save new list of crop pairs
        with open(self.paths.cropped_pairs_path, "w") as f:
            for p1, p2 in crop_pairs:
                f.write(f"{p1} {p2}\n")
        
        logging.info("Performing feature extraction and matching on crops")
        extract_features.main(
            conf=self.config["features"][0] if self.is_ensemble else self.config["features"],
            image_dir=self.paths.cropped_image_dir,
            feature_path=self.paths.cropped_features_path,
        )
        match_features.main(
            conf=self.config["matches"][0] if self.is_ensemble else self.config["matches"],
            pairs=self.paths.cropped_pairs_path,
            features=self.paths.cropped_features_path,
            matches=self.paths.cropped_matches_path,
        )

        logging.info("Transforming keypoints from cropped image spaces to original image spaces")
        with h5py.File(str(self.paths.cropped_features_path), "r+", libver="latest") as f:
            for name in offsets.keys():
                keypoints = f[name]["keypoints"].__array__()
                keypoints[:,0] += offsets[name][0]
                keypoints[:,1] += offsets[name][1]
                f[name]["keypoints"][...] = keypoints

        logging.info("Concatenating features and matches from crops with original features and matches")
        # THE CONCATENATION IS CURRENTLY WRONG!!!!!!!!!!
        concat_features(self.paths.features_path, self.paths.cropped_features_path, self.paths.features_path)
        concat_matches(self.paths.matches_path, self.paths.cropped_matches_path, self.paths.matches_path)

    def back_rotate_cameras(self):
        """Rotate R and t for each rotated camera. """
        if not self.use_rotation_wrapper:
            return
        self.log_step("Back-rotate camera poses")
        for id, im in self.sparse_model.images.items():
            angle = self.rotation_angles[im.name]
            if angle !=0:
                # back rotate <Image 'image_id=30, camera_id=30, name="DSC_6633.JPG", triangulated=404/3133'> by 90
                # logging.info(f"back rotate {im} by {angle}")
                rotmat = rot_mat_z(angle)
                # logging.info(rotmat)
                R = im.rotmat()
                t = np.array(im.tvec)
                self.sparse_model.images[id].tvec = rotmat @t
                self.sparse_model.images[id].qvec = rotmat2qvec(rotmat @ R)
        # self.sparse_model.write(self.paths.sfm_dir)
        # swap the two image folders
        image_dir = self.paths.rotated_image_dir
        self.paths.rotated_image_dir = self.paths.image_dir
        self.paths.image_dir = image_dir

    def rotate_keypoints(self) -> None:
        """Rotate keypoints back after the rotation matching."""
        if not self.use_rotation_matching:
            return

        self.log_step("Rotating keypoints")

        logging.info(f"Using rotated features from {self.paths.rotated_features_path}")
        shutil.copy(self.paths.rotated_features_path, self.paths.features_path)

        logging.info(f"Writing rotated keypoints to {self.paths.features_path}")
        with h5py.File(str(self.paths.features_path), "r+", libver="latest") as f:
            for image_fn, angle in self.rotation_angles.items():
                if angle == 0:
                    continue

                keypoints = f[image_fn]["keypoints"].__array__()
                y_max, x_max = cv2.imread(str(self.paths.rotated_image_dir / image_fn)).shape[:2]

                new_keypoints = np.zeros_like(keypoints)
                if angle == 90:
                    # rotate keypoints by -90 degrees
                    # ==> (x,y) becomes (y, x_max - x)
                    new_keypoints[:, 0] = keypoints[:, 1]
                    new_keypoints[:, 1] = x_max - keypoints[:, 0]-1
                elif angle == 180:
                    # rotate keypoints by 180 degrees
                    # ==> (x,y) becomes (x_max - x, y_max - y)
                    new_keypoints[:, 0] = x_max - keypoints[:, 0]-1
                    new_keypoints[:, 1] = y_max - keypoints[:, 1]-1
                elif angle == 270:
                    # rotate keypoints by +90 degrees
                    # ==> (x,y) becomes (y_max - y, x)
                    new_keypoints[:, 0] = y_max - keypoints[:, 1]-1
                    new_keypoints[:, 1] = keypoints[:, 0]
                f[image_fn]["keypoints"][...] = new_keypoints

    def sfm(self) -> None:
        """Run Structure from Motion."""
        self.log_step("Run SfM")

        if self.paths.sfm_dir.exists() and not self.overwrite:
            try:
                self.sparse_model = pycolmap.Reconstruction(self.paths.sfm_dir)
                logging.info(f"Sparse model already at {self.paths.sfm_dir}")
                return
            except ValueError:
                self.sparse_model = None

        logging.info(f"Using images from {self.paths.image_dir}")
        logging.info(f"Using pairs from {self.paths.pairs_path}")
        logging.info(f"Using features from {self.paths.features_path}")
        logging.info(f"Using matches from {self.paths.matches_path}")

        if self.use_pixsfm and len(self.img_list) <= self.pixsfm_max_imgs:
            logging.info("Using PixSfM")

            if not self.paths.cache.exists():
                self.paths.cache.mkdir(parents=True)

            proc = subprocess.Popen(
                [
                    "python",
                    self.pixsfm_script_path,
                    "--sfm_dir",
                    str(self.paths.sfm_dir),
                    "--image_dir",
                    str(self.paths.image_dir),
                    "--pairs_path",
                    str(self.paths.pairs_path),
                    "--features_path",
                    str(self.paths.features_path),
                    "--matches_path",
                    str(self.paths.matches_path),
                    "--cache_path",
                    str(self.paths.cache),
                    "--pixsfm_config",
                    self.pixsfm_config,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                logging.info(
                    "Running PixSfM in subprocess (no console output until PixSfM finishes)"
                )
                output, error = proc.communicate()
                logging.info(output.decode())
                logging.error(error.decode())

                # subprocess writes sfm model to disk => load model in main process
                if self.paths.sfm_dir.exists():
                    try:
                        self.sparse_model = pycolmap.Reconstruction(self.paths.sfm_dir)
                    except ValueError:
                        self.sparse_model = None
            except Exception:
                logging.warning("Could not reconstruct model with PixSfM.")
                self.sparse_model = None
        else: 
            self.sparse_model = reconstruction.main(
                sfm_dir=self.paths.sfm_dir,
                image_dir=self.paths.image_dir,
                image_list=self.img_list,
                pairs=self.paths.pairs_path,
                features=self.paths.features_path,
                matches=self.paths.matches_path,
                verbose=False,
            )

        if self.sparse_model is not None:
            self.sparse_model.write(self.paths.sfm_dir)

    def localize_unregistered(self) -> None:
        """Try to localize unregistered images."""
        pass

    def run(self) -> None:
        """Run the pipeline."""
        self.timing = {
            "preprocessing": time_function(self.preprocess)(),
            "pairs-extraction": time_function(self.get_pairs)(),
            "feature-extraction": time_function(self.extract_features)(),
            "feature-matching": time_function(self.match_features)(),
            "create-ensemble": time_function(self.create_ensemble)(),
            "image-cropping": time_function(self.perform_cropping)(),
            "rotate-keypoints": time_function(self.rotate_keypoints)(),
            "sfm": time_function(self.sfm)(),
            "back-rotate-cameras":time_function(self.back_rotate_cameras)(),
            "localize-unreg": time_function(self.localize_unregistered)(),
        }
