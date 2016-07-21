# See COPYRIGHT file at the top of the source tree.

from __future__ import division, absolute_import, print_function

import os
import numpy as np

import lsst.utils
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.afw.image as afwImage
import lsst.afw.table as afwTable
import lsst.afw.geom as afwGeom
import lsst.afw.coord as afwCoord
import lsst.pex.exceptions as pexExceptions

from lsst.meas.astrom.loadAstrometryNetObjects import LoadAstrometryNetObjectsTask
from lsst.meas.astrom import AstrometryNetDataConfig
from lsst.meas.algorithms.sourceSelector import sourceSelectorRegistry

from .dataIds import PerTractCcdDataIdContainer

from . import jointcalLib

__all__ = ["JointcalConfig", "JointcalTask"]


class JointcalRunner(pipeBase.TaskRunner):
    """Subclass of TaskRunner for jointcalTask (copied from the HSC MosaicRunner)

    jointcalTask.run() takes a number of arguments, one of which is a list of dataRefs
    extracted from the command line (whereas most CmdLineTasks' run methods take
    single dataRef, are are called repeatedly).  This class transforms the processed
    arguments generated by the ArgumentParser into the arguments expected by
    MosaicTask.run().

    See pipeBase.TaskRunner for more information, but note that the multiprocessing
    code path does not apply, because MosaicTask.canMultiprocess == False.
    """

    @staticmethod
    def getTargetList(parsedCmd, **kwargs):
        # organize data IDs by tract
        refListDict = {}
        for ref in parsedCmd.id.refList:
            refListDict.setdefault(ref.dataId["tract"], []).append(ref)
        # we call run() once with each tract
        return [(refListDict[tract],) for tract in sorted(refListDict.keys())]

    def __call__(self, args):
        """
        @param args     Arguments for Task.run()

        @return
        - None if self.doReturnResults is False
        - A pipe.base.Struct containing these fields if self.doReturnResults is True:
            - dataRef: the provided data references, with update post-fit WCS's.
        """
        task = self.TaskClass(config=self.config, log=self.log)
        result = task.run(*args)
        if self.doReturnResults:
            return pipeBase.Struct(result = result)


class JointcalConfig(pexConfig.Config):
    """Config for jointcalTask"""

    coaddName = pexConfig.Field(
        doc = "Type of coadd",
        dtype = str,
        default = "deep"
    )
    posError = pexConfig.Field(
        doc = "Constant term for error on position (in pixel unit)",
        dtype = float,
        default = 0.02,
    )
    polyOrder = pexConfig.Field(
        doc = "Polynomial order for fitting distorsion",
        dtype = int,
        default = 3,
    )
    sourceFluxType = pexConfig.Field(
        doc = "Type of source flux (e.g. Ap, Psf, Calib): passed to sourceSelector "
              "and used in ccdImage",
        dtype = str,
        default = "Calib"
    )
    sourceSelector = sourceSelectorRegistry.makeField(
        doc = "How to select sources for cross-matching",
        default = "astrometry"
    )
    # TODO: CmdLineTask has a profile thing built-in: can we tie in to that?
    profile = pexConfig.Field(
        doc = "Profile jointcal, including the catalog creation step.",
        dtype = bool,
        default = False
    )

    def setDefaults(self):
        sourceSelector = self.sourceSelector["astrometry"]
        sourceSelector.setDefaults()
        sourceSelector.sourceFluxType = self.sourceFluxType
        # don't want to lose existing flags, just add to them.
        sourceSelector.badFlags.extend(["slot_Shape_flag"])


class JointcalTask(pipeBase.CmdLineTask):
    """Jointly astrometrically (photometrically later) calibrate a group of images."""

    ConfigClass = JointcalConfig
    RunnerClass = JointcalRunner
    _DefaultName = "jointcal"

    def __init__(self, *args, **kwargs):
        pipeBase.CmdLineTask.__init__(self, *args, **kwargs)
        self.makeSubtask("sourceSelector")

    # We don't need to persist config and metadata at this stage.
    # In this way, we don't need to put a specific entry in the camera mapper policy file
    def _getConfigName(self):
        return None

    def _getMetadataName(self):
        return None

    @classmethod
    def _makeArgumentParser(cls):
        """Create an argument parser"""
        parser = pipeBase.ArgumentParser(name=cls._DefaultName)

        parser.add_id_argument("--id", "calexp", help="data ID, e.g. --selectId visit=6789 ccd=0..9",
                               ContainerClass=PerTractCcdDataIdContainer)
        return parser

    def _build_ccdImage(self, dataRef, associations, jointcalControl):
        """
        Extract the necessary things from this dataRef to add a new ccdImage.

        @param dataRef (ButlerDataRef) dataRef to extract info from.
        @param associations (jointcal.Associations) object to add the info to, to
            construct a new CcdImage
        @param jointcalControl (jointcal.JointcalControl) control object for
            associations management

        @return (afw.image.TanWcs) the TAN WCS of this image
        """
        src = dataRef.get("src", immediate=True)
        md = dataRef.get("calexp_md", immediate=True)
        tanwcs = afwImage.TanWcs.cast(afwImage.makeWcs(md))
        lLeft = afwImage.getImageXY0FromMetadata(afwImage.wcsNameForXY0, md)
        uRight = afwGeom.Point2I(lLeft.getX() + md.get("NAXIS1")-1, lLeft.getY() + md.get("NAXIS2")-1)
        bbox = afwGeom.Box2I(lLeft, uRight)
        calib = afwImage.Calib(md)
        filt = dataRef.dataId['filter']

        goodSrc = self.sourceSelector.selectSources(src)

        # ------------------
        # TODO: Leaving the old catalog and comparison code in while we
        # sort out the old/new star selector differences.

        # configSel = StarSelectorConfig()
        # self.oldSS = StarSelector(configSel)
        # stars1 = goodSrc.sourceCat.copy(deep=True)
        # stars2 = self.oldSS.select(src, calib).copy(deep=True)
        # fluxField = jointcalControl.sourceFluxField
        # SN = stars1.get(fluxField+"_flux") / stars1.get(fluxField+"_fluxSigma")
        # aa = [x for x in stars1['id'] if x in stars2['id']]
        # print("new, old, shared:", len(stars1), len(stars2), len(aa))
        # # import ipdb; ipdb.set_trace()

        # print("%d stars selected in visit %d - ccd %d"%(len(stars2),
        #                                                 dataRef.dataId["visit"],
        #                                                 dataRef.dataId["ccd"]))
        # associations.AddImage(stars2, tanwcs, md, bbox, filt, calib,
        #                       dataRef.dataId['visit'], dataRef.dataId['ccd'],
        #                       dataRef.getButler().get("camera").getName(),
        #                       jointcalControl)
        # TODO: End of old source selector debugging block.
        # ------------------

        if len(goodSrc.sourceCat) == 0:
            print("no stars selected in ", dataRef.dataId["visit"], dataRef.dataId["ccd"])
            return
        print("%d stars selected in visit %d - ccd %d"%(len(goodSrc.sourceCat),
                                                        dataRef.dataId["visit"],
                                                        dataRef.dataId["ccd"]))

        associations.AddImage(goodSrc.sourceCat, tanwcs, md, bbox, filt, calib,
                              dataRef.dataId['visit'], dataRef.dataId['ccd'],
                              dataRef.getButler().get("camera").getName(),
                              jointcalControl)
        return tanwcs

    @pipeBase.timeMethod
    def run(self, dataRefs):
        """
        !Jointly calibrate the astrometry and photometry across a set of images.

        @param dataRefs list of data references.

        @return (pipe.base.Struct) dataRefs that were fit (with updated WCSs) and old WCSs.
        """
        if len(dataRefs) == 0:
            raise ValueError('Need a list of data references!')

        jointcalControl = jointcalLib.JointcalControl()
        jointcalControl.sourceFluxField = 'slot_'+self.config.sourceFluxType+'Flux'

        associations = jointcalLib.Associations()

        if self.config.profile:
            import cProfile
            import pstats
            profile = cProfile.Profile()
            profile.enable()
            for dataRef in dataRefs:
                self._build_ccdImage(dataRef, associations, jointcalControl)
            profile.disable()
            profile.dump_stats('jointcal_load_catalog.prof')
            prof = pstats.Stats('jointcal_load_catalog.prof')
            prof.strip_dirs().sort_stats('cumtime').print_stats(20)
        else:
            old_wcss = []
            for dataRef in dataRefs:
                old_wcss.append(self._build_ccdImage(dataRef, associations, jointcalControl))

        matchCut = 3.0
        # TODO: this should not print "trying to invert a singular transformation:"
        # if it does that, something's not right about the WCS...
        associations.AssociateCatalogs(matchCut)

        # Use external reference catalogs handled by LSST stack mechanism
        # Get the bounding box overlapping all associated images
        # ==> This is probably a bad idea to do it this way <== To be improved
        bbox = associations.GetRaDecBBox()
        center = afwCoord.Coord(bbox.getCenter(), afwGeom.degrees)
        corner = afwCoord.Coord(bbox.getMax(), afwGeom.degrees)
        radius = center.angularSeparation(corner).asRadians()

        # Get astrometry_net_data path
        anDir = lsst.utils.getPackageDir('astrometry_net_data')
        if anDir is None:
            raise RuntimeError("astrometry_net_data is not setup")

        andConfig = AstrometryNetDataConfig()
        andConfigPath = os.path.join(anDir, "andConfig.py")
        if not os.path.exists(andConfigPath):
            raise RuntimeError("astrometry_net_data config file \"%s\" required but not found"%andConfigPath)
        andConfig.load(andConfigPath)

        task = LoadAstrometryNetObjectsTask.ConfigClass()
        loader = LoadAstrometryNetObjectsTask(task)

        # TODO: I don't think this is the "default" filter...
        # Determine default filter associated to the catalog
        filt, mfilt = andConfig.magColumnMap.items()[0]
        print("Using", filt, "band for reference flux")

        refCat = loader.loadSkyCircle(center, afwGeom.Angle(radius, afwGeom.radians), filt).refCat

        # associations.CollectRefStars(False) # To use USNO-A catalog

        associations.CollectLSSTRefStars(refCat, filt)
        associations.SelectFittedStars()
        associations.DeprojectFittedStars()  # required for AstromFit
        sky2TP = jointcalLib.OneTPPerShoot(associations.TheCcdImageList())
        spm = jointcalLib.SimplePolyModel(associations.TheCcdImageList(), sky2TP,
                                          True, 0, self.config.polyOrder)

        # TODO: these should be len(blah), but we need this properly wrapped first.
        if associations.refStarListSize() == 0:
            raise RuntimeError('No stars in the reference star list!')
        if len(associations.ccdImageList) == 0:
            raise RuntimeError('No images in the ccdImageList!')
        if associations.fittedStarListSize() == 0:
            raise RuntimeError('No stars in the fittedStarList!')

        fit = jointcalLib.AstromFit(associations, spm, self.config.posError)
        fit.Minimize("Distortions")
        chi2 = fit.ComputeChi2()
        print(chi2)
        fit.Minimize("Positions")
        chi2 = fit.ComputeChi2()
        print(chi2)
        fit.Minimize("Distortions Positions")
        chi2 = fit.ComputeChi2()
        print(chi2)

        for i in range(20):
            r = fit.Minimize("Distortions Positions", 5)  # outliers removal at 5 sigma.
            chi2 = fit.ComputeChi2()
            print(chi2)
            if r == 0:
                print("""fit has converged - no more outliers - redo minimixation\
                      one more time in case we have lost accuracy in rank update""")
                # Redo minimization one more time in case we have lost accuracy in rank update
                r = fit.Minimize("Distortions Positions", 5)  # outliers removal at 5 sigma.
                chi2 = fit.ComputeChi2()
                print(chi2)
                break
            elif r == 2:
                print("minimization failed")
            elif r == 1:
                print("still some ouliers but chi2 increases - retry")
            else:
                break
                print("unxepected return code from Minimize")

        # Fill reference and measurement n-tuples for each tract
        tupleName = "res_" + str(dataRef.dataId["tract"]) + ".list"
        fit.MakeResTuple(tupleName)

        # Build an updated wcs for each calexp
        imList = associations.TheCcdImageList()

        for im in imList:
            tanSip = spm.ProduceSipWcs(im)
            frame = im.ImageFrame()
            tanWcs = afwImage.TanWcs.cast(jointcalLib.GtransfoToTanWcs(tanSip, frame, False))

            name = im.Name()
            visit, ccd = name.split('_')
            for dataRef in dataRefs:
                if dataRef.dataId["visit"] == int(visit) and dataRef.dataId["ccd"] == int(ccd):
                    print("Updating WCS for visit: %d, ccd%d"%(int(visit), int(ccd)))
                    exp = afwImage.ExposureI(0, 0)
                    exp.setWcs(tanWcs)
                    try:
                        dataRef.put(exp, 'wcs')
                    except pexExceptions.Exception as e:
                        self.log.warn('Failed to write updated Wcs: ' + str(e))
                    break

        return pipeBase.Struct(dataRefs=dataRefs, old_wcss=old_wcss)


# TODO: Leaving StarSelector[Config] here for reference.
# TODO: We can remove them once we're happy with astrometryStarSelector.

class StarSelectorConfig(pexConfig.Config):

    badFlags = pexConfig.ListField(
        doc = "List of flags which cause a source to be rejected as bad",
        dtype = str,
        default = ["base_PixelFlags_flag_saturated",
                   "base_PixelFlags_flag_cr",
                   "base_PixelFlags_flag_interpolated",
                   "base_SdssCentroid_flag",
                   "base_SdssShape_flag"],
    )
    sourceFluxField = pexConfig.Field(
        doc = "Type of source flux",
        dtype = str,
        default = "slot_CalibFlux"
    )
    maxMag = pexConfig.Field(
        doc = "Maximum magnitude for sources to be included in the fit",
        dtype = float,
        default = 22.5,
    )
    coaddName = pexConfig.Field(
        doc = "Type of coadd",
        dtype = str,
        default = "deep"
    )
    centroid = pexConfig.Field(
        doc = "Centroid type for position estimation",
        dtype = str,
        default = "base_SdssCentroid",
    )
    shape = pexConfig.Field(
        doc = "Shape for error estimation",
        dtype = str,
        default = "base_SdssShape",
    )


class StarSelector(object):

    ConfigClass = StarSelectorConfig

    def __init__(self, config):
        """Construct a star selector

        @param[in] config: An instance of StarSelectorConfig
        """
        self.config = config

    def select(self, srcCat, calib):
        """Return a catalog containing only reasonnable stars / galaxies."""

        schema = srcCat.getSchema()
        newCat = afwTable.SourceCatalog(schema)
        fluxKey = schema[self.config.sourceFluxField+"_flux"].asKey()
        fluxErrKey = schema[self.config.sourceFluxField+"_fluxSigma"].asKey()
        parentKey = schema["parent"].asKey()
        flagKeys = []
        for f in self.config.badFlags:
            key = schema[f].asKey()
            flagKeys.append(key)
        fluxFlagKey = schema[self.config.sourceFluxField+"_flag"].asKey()
        flagKeys.append(fluxFlagKey)

        for src in srcCat:
            # Do not consider sources with bad flags
            for f in flagKeys:
                rej = 0
                if src.get(f):
                    rej = 1
                    break
            if rej == 1:
                continue
            # Reject negative flux
            flux = src.get(fluxKey)
            if flux < 0:
                continue
            # Reject objects with too large magnitude
            fluxErr = src.get(fluxErrKey)
            mag, magErr = calib.getMagnitude(flux, fluxErr)
            if mag > self.config.maxMag or magErr > 0.1 or flux/fluxErr < 10:
                continue
            # Reject blends
            if src.get(parentKey) != 0:
                continue
            footprint = src.getFootprint()
            if footprint is not None and len(footprint.getPeaks()) > 1:
                continue

            # Check consistency of variances and second moments
            vx = np.square(src.get(self.config.centroid + "_xSigma"))
            vy = np.square(src.get(self.config.centroid + "_ySigma"))
            mxx = src.get(self.config.shape + "_xx")
            myy = src.get(self.config.shape + "_yy")
            mxy = src.get(self.config.shape + "_xy")
            vxy = mxy*(vx+vy)/(mxx+myy)

            if vxy*vxy > vx*vy or np.isnan(vx) or np.isnan(vy):
                continue

            newCat.append(src)

        return newCat
