from argschema import ArgSchemaParser
import renderapi
from .meta_to_collection import main as mtc_main
from .schemas import MontageSolverSchema
from ..utils.generate_EM_tilespecs_from_metafile import \
        GenerateEMTileSpecsModule
from ..utils.utils import pointmatch_filter, get_z_from_metafile
from EMaligner import jsongz
import EMaligner.EMaligner as ema
import json
import os
import glob
import copy
import time


example = {
        "data_dir": "/data/em-131fs3/lctest/T4_6/001844/0",
        "output_dir": "/data/em-131fs3/lctest/T4_6/001844/0",
        "ref_transform": None,
        "ransacReprojThreshold": 10
        }

dname = os.path.dirname(os.path.abspath(__file__))
montage_template = os.path.join(
        dname, 'templates', 'montage_template.json')


def do_solve(args, transform, outname, lam):
    with open(montage_template, 'r') as f:
        template = json.load(f)
    template['input_stack']['input_file'] = \
        args['input_stack']['input_file']
    template['pointmatch']['input_file'] = \
        args['pointmatch']['input_file']
    template['output_stack']['compress_output'] = \
        args['output_stack']['compress_output']
    template['first_section'] = template['last_section'] = \
        args['first_section']
    template['transformation'] = transform
    template['regularization']['default_lambda'] = lam
    template['output_stack']['output_file'] = os.path.join(
            os.path.dirname(args['input_stack']['input_file']),
            outname)
    aligner = ema.EMaligner(input_data=template, args=[])
    aligner.run()


def do_solves(collection, input_stack, z, compress):
    args = {'input_stack': {}, 'output_stack': {}, 'pointmatch': {}}
    args['input_stack']['input_file'] = input_stack
    args['pointmatch']['input_file'] = collection
    args['output_stack']['compress_output'] = compress
    args['first_section'] = args['last_section'] = z

    do_solve(
            copy.deepcopy(args),
            'AffineModel',
            'resolved_output_tiles_affine.json',
            5e5)

    do_solve(
            copy.deepcopy(args),
            'Polynomial2DTransform',
            'resolved_output_tiles_poly.json',
            1000)


def check_failed_from_metafile(metafile):
    with open(metafile, 'r') as f:
        j = json.load(f)
    return j[2]['tile_qc']['failed']


def montage_filter_matches(matches, thresh, model='Similarity'):
    for match in matches:
        _, _, w, _ = pointmatch_filter(
                match,
                n_clusters=1,
                n_cluster_pts=6,
                ransacReprojThreshold=thresh,
                model=model)
        match['matches']['w'] = w.tolist()


def get_metafile_path(datadir):
    return glob.glob(os.path.join(datadir, '_metadata*.json'))[0]


def make_raw_tilespecs(metafile, outputdir, groupId, compress):
    z = get_z_from_metafile(metafile)
    tspecin = {
            "metafile": metafile,
            "z": z,
            "sectionId": groupId,
            "output_path": os.path.join(outputdir, 'raw_tilespecs.json'),
            "compress_output": compress
            }
    gmod = GenerateEMTileSpecsModule(input_data=tspecin, args=[])
    gmod.run()
    return gmod.args['output_path'], z


def get_transform(metafile, tfpath, from_meta):
    if from_meta:
        with open(metafile, 'r') as f:
            j = json.load(f)
        tfj = j[2]['sharedTransform']
    else:
        with open(tfpath, 'r') as f:
            tfj = json.load(f)
    return renderapi.transform.Transform(json=tfj)


def make_resolved(rawspecpath, tform, outputdir, compress):
    # read in the tilespecs
    rtj = jsongz.load(rawspecpath)
    tspecs = [renderapi.tilespec.TileSpec(json=t) for t in rtj]

    # do not need this anymore
    os.remove(rawspecpath)

    # add the reference transform
    ref = renderapi.transform.ReferenceTransform()
    ref.refId = tform.transformId
    for t in tspecs:
        t.tforms.insert(0, ref)

    # make a resolved tile object
    resolved = renderapi.resolvedtiles.ResolvedTiles(
            tilespecs=tspecs,
            transformList=[tform])

    # write it to file and return the path
    rpath = os.path.join(outputdir, 'resolved_input_tiles.json')

    return jsongz.dump(resolved.to_dict(), rpath, compress)


class MontageSolver(ArgSchemaParser):
    default_schema = MontageSolverSchema

    def run(self):
        if not self.args['output_dir']:
            self.args['output_dir'] = self.args['data_dir']

        # read the matches from the metafile
        matches = mtc_main([self.args['data_dir']])

        montage_filter_matches(
                matches,
                self.args['ransacReprojThreshold'])

        # write to file
        collection = os.path.join(self.args['output_dir'], "collection.json")
        collection = jsongz.dump(
                matches,
                collection,
                compress=self.args['compress_output'])

        metafile = get_metafile_path(self.args['data_dir'])

        # make raw tilespec json
        rawspecpath, z = make_raw_tilespecs(
                metafile,
                self.args['output_dir'],
                matches[0]['pGroupId'],
                self.args['compress_output'])

        # get the ref transform
        tform = get_transform(
                metafile,
                self.args['ref_transform'],
                self.args['read_transform_from_meta'])

        # make a resolved tile object
        input_stack_path = make_resolved(
                rawspecpath,
                tform,
                self.args['output_dir'],
                self.args['compress_output'])

        do_solves(
                collection,
                input_stack_path,
                z,
                self.args['compress_output'])


if __name__ == "__main__":
    t0 = time.time()
    mm = MontageSolver(input_data=example)
    mm.run()
    print('total time %0.1f sec' % (time.time() - t0))
