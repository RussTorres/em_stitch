import glob
import json
import logging
import os
import warnings

import cv2
import numpy as np

from argschema import ArgSchemaParser
from bigfeta import jsongz
import renderapi

from .schemas import LensCorrectionSchema
from ..utils.generate_EM_tilespecs_from_metafile import (
    GenerateEMTileSpecsModule)
from ..utils import utils as common_utils
from .mesh_and_solve_transform import MeshAndSolveTransform
from . import utils

warnings.simplefilter(action='ignore', category=FutureWarning)

logger = logging.getLogger(__name__)

example = {
        "data_dir": "/data/em-131fs3/lctest/T4_07/20190315142205_reference/0",
        "output_dir": "/data/em-131fs3/lctest/T4_07/20190315142205_reference/0",
        "mask_file": None,
        "ransac_thresh": 10,
        "nvertex": 1000,
        "regularization": {
            "default_lambda": 1.0,
            "lens_lambda": 1.0,
            "translation_factor": 1e-3
            },
        "log_level": "INFO",
        "write_pdf": False
        }


class LensCorrectionException(Exception):
    pass


def one_file(fdir, fstub):
    """
    Get a single file matching a directory and filename pattern.

    Parameters
    ----------
    fdir : str
        The directory where the file should be located.
    fstub : str
        The filename pattern to match.

    Returns
    -------
    str
        The full path of the single file matching the pattern.

    Raises
    ------
    LensCorrectionException
        If no file or more than one file is found matching the pattern.
    """
    fullstub = os.path.join(fdir, fstub)
    files = glob.glob(fullstub)
    lf = len(files)
    if lf != 1:
        raise LensCorrectionException(
            "expected 1 file, found %d for %s" % (
                lf,
                fullstub))
    return files[0]


def tilespec_input_from_metafile(
        metafile, mask_file, output_dir, log_level, compress):
    """
    get tilespec input data from a metafile.

    Parameters
    ----------
    metafile : str
        Path to the metafile.
    mask_file : str
        Path to the mask file.
    output_dir : str
        Directory where the output will be stored.
    log_level : int
        Log level for the operation.
    compress : bool
        Whether to compress the output.

    Returns
    -------
    Dict[str, Union[str, int, bool]]
        A dictionary containing the generated tilespec input data.
    """
    result = {}
    result['metafile'] = metafile

    with open(metafile, 'r') as f:
        jmeta = json.load(f)

    result['z'] = np.random.randint(0, 1000)
    result['minimum_intensity'] = 0
    bpp = jmeta[0]['metadata']['camera_info']['camera_bpp']
    result['maximum_intensity'] = int(np.power(2, bpp) - 1)
    result['sectionId'] = jmeta[0]['metadata']['grid']
    result['maskUrl'] = mask_file
    result['output_path'] = os.path.join(output_dir, 'raw_tilespecs.json')
    result['log_level'] = log_level
    result['compress_output'] = compress
    return result


def filter_match_collection(
        matches, threshold, model="Similarity",
        n_clusters=None, n_cluster_pts=20, ransacReprojThreshold=40.,
        ignore_match_indices=(),
        input_n_key="n_from_gpu", output_n_key="n_after_filter"):
    """
    Filter a collection of matches based on specified criteria.

    Parameters
    ----------
    matches : List[Dict[str, Any]]
        The collection of matches to filter.
    threshold : float
        Threshold value.
    model : str, optional
        Model type, by default "Similarity".
    n_clusters : int, optional
        Number of clusters, by default None.
    n_cluster_pts : int, optional
        Number of cluster points, by default 20.
    ransacReprojThreshold : float, optional
        RANSAC reprojection threshold, by default 40.0.
    ignore_match_indices : Optional[Iterable[int]], optional
        Indices of matches to ignore, by default None.
    input_n_key : str, optional
        Key to store input count in counts dictionary, by default "n_from_gpu".
    output_n_key : str, optional
        Key to store output count in counts dictionary, by default "n_after_filter".

    Returns
    -------
    Tuple[List[Dict[str, Any]], List[Dict[str, int]]]
        A tuple containing the filtered matches and their corresponding counts.
    """
    ignore_match_indices = (set() if ignore_match_indices is None else ignore_match_indices)
    ignore_match_indices = set(ignore_match_indices)

    counts = []
    new_matches = []

    for i, m in enumerate(matches):
        input_n = len(m["matches"]["p"][0])

        _, _, w, _ = common_utils.pointmatch_filter(
            m,
            n_clusters=n_clusters,
            n_cluster_pts=n_cluster_pts,

            )

        m["matches"]["w"] = w.tolist()

        output_n = np.count_nonzero(w)

        counts.append({
            input_n_key: input_n,
            output_n_key: output_n})

        # original version ran pointmatch filtering for these and then filtered
        #   them out of the matches.  Do this by continuing here, I guess.
        if i in ignore_match_indices:
            continue

        new_matches.append(m)

    return new_matches, counts


def make_collection_json(
        template_file,
        output_dir,
        thresh,  # FIXME thresh not used in this version
        compress,
        ignore_match_indices=None):
    """
    Create a JSON collection file from a template file.

    Parameters
    ----------
    template_file : str
        Path to the template file.
    output_dir : str
        Directory where the output will be stored.
    thresh : float
        Threshold value.
    compress : bool
        Whether to compress the output.
    ignore_match_indices : Optional[List[int]], optional
        Indices of matches to ignore, by default None.

    Returns
    -------
    Tuple[str, List[Dict[str, int]]]
        A tuple containing the path to the collection file and a list of counts.
    """
    with open(template_file, 'r') as f:
        template_match_md = json.load(f)

    input_matches = template_match_md["collection"]

    m, counts = filter_match_collection(
        input_matches, thresh, 
        ignore_match_indices=ignore_match_indices
    )

    collection_file = os.path.join(output_dir, "collection.json")
    collection_file = jsongz.dump(m, collection_file, compress=compress)

    return collection_file, counts


class LensCorrectionSolver(ArgSchemaParser):
    default_schema = LensCorrectionSchema

    def __init__(self, *args, **kwargs):
        super(LensCorrectionSolver, self).__init__(*args, **kwargs)
        self.jtform = None

    def run(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(self.args['log_level'])
        utils.logger.setLevel(self.args['log_level'])
        self.check_for_files()
        self.output_dir = self.args.get('output_dir', self.args['data_dir'])
        self.logger.info("destination directory:\n  %s" % self.output_dir)

        tspecin = tilespec_input_from_metafile(
                self.metafile,
                self.args['mask_file'],
                self.output_dir,
                self.args['log_level'],
                self.args['compress_output'])
        gentspecs = GenerateEMTileSpecsModule(input_data=tspecin, args=[])
        gentspecs.run()

        assert os.path.isfile(gentspecs.args['output_path'])
        self.logger.info(
                "raw tilespecs written:\n  %s" % gentspecs.args['output_path'])

        collection_path, self.filter_counts = make_collection_json(
                self.matchfile,
                self.output_dir,
                self.args['ransac_thresh'],
                self.args['compress_output'],
                self.args['ignore_match_indices'])

        self.n_from_gpu = np.array(
                [i['n_from_gpu'] for i in self.filter_counts]).sum()
        self.n_after_filter = np.array(
                [i['n_after_filter'] for i in self.filter_counts]).sum()
        self.logger.info(
                "filter counts: %0.2f %% kept" %
                (100 * float(self.n_after_filter) / self.n_from_gpu))

        assert os.path.isfile(collection_path)
        self.logger.info(
                "filtered collection written:\n  %s" % collection_path)

        solver_args = {
                'nvertex': self.args['nvertex'],
                'regularization': self.args['regularization'],
                'good_solve': self.args['good_solve'],
                'tilespecs': gentspecs.tilespecs,
                'match_file': collection_path,
                'output_dir': self.output_dir,
                'outfile': 'resolvedtiles.json.gz',
                'compress_output': self.args['compress_output'],
                'log_level': self.args['log_level'],
                'timestamp': self.args['timestamp']}

        self.solver = MeshAndSolveTransform(input_data=solver_args, args=[])
        self.solver.run()

        with open(self.solver.args['output_json'], 'r') as f:
            j = json.load(f)
        resolved_path = j['resolved_tiles']

        resolvedtiles = renderapi.resolvedtiles.ResolvedTiles(
                json=jsongz.load(resolved_path))

        self.jtform = resolvedtiles.transforms[0].to_dict()

        self.map1, self.map2, self.mask = utils.maps_from_tform(
                renderapi.transform.ThinPlateSplineTransform(
                    json=self.jtform),
                resolvedtiles.tilespecs[0].width,
                resolvedtiles.tilespecs[0].height,
                res=32)

        maskname = os.path.join(self.output_dir, 'mask.png')
        cv2.imwrite(maskname, self.mask)
        self.logger.info("wrote:\n  %s" % maskname)

        res = {}
        res['input'] = {}
        res['input']['template'] = os.path.abspath(self.matchfile)
        res['input']['metafile'] = os.path.abspath(self.metafile)
        res['output'] = {}
        res['output']['resolved_tiles'] = j.pop('resolved_tiles')
        res['output']['mask'] = os.path.abspath(maskname)
        res['output']['collection'] = os.path.abspath(collection_path)
        res['residual stats'] = j

        self.args['output_json'] = self.solver.args['output_json']

        with open(self.args['output_json'], 'w') as f:
            json.dump(res, f, indent=2)

    def check_for_files(self):
        self.metafile = one_file(self.args['data_dir'], '_metadata*')
        self.matchfile = one_file(self.args['data_dir'], '_template_matches*')
        self.logger.info("Using files: \n  %s\n  %s" % (
            self.metafile, self.matchfile))


if __name__ == '__main__':
    lcmod = LensCorrectionSolver(input_data=example)
    lcmod.run()
