# See COPYRIGHT file at the top of the source tree.

from __future__ import division, absolute_import, print_function
from builtins import str
from builtins import range

import collections

import lsst.utils
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.afw.image as afwImage
import lsst.afw.geom as afwGeom
import lsst.afw.coord as afwCoord
import lsst.pex.exceptions as pexExceptions
import lsst.afw.table
import lsst.meas.algorithms

from lsst.meas.algorithms import LoadIndexedReferenceObjectsTask
from lsst.meas.algorithms.sourceSelector import sourceSelectorRegistry

from .dataIds import PerTractCcdDataIdContainer

import lsst.jointcal
from lsst.jointcal import MinimizeResult

__all__ = ["JointcalConfig", "JointcalTask"]

Photometry = collections.namedtuple('Photometry', ('fit', 'model'))
Astrometry = collections.namedtuple('Astrometry', ('fit', 'model', 'sky_to_tan_projection'))


class JointcalRunner(pipeBase.ButlerInitializedTaskRunner):
    """Subclass of TaskRunner for jointcalTask

    jointcalTask.run() takes a number of arguments, one of which is a list of dataRefs
    extracted from the command line (whereas most CmdLineTasks' run methods take
    single dataRef, are are called repeatedly). This class transforms the processed
    arguments generated by the ArgumentParser into the arguments expected by
    Jointcal.run().

    See pipeBase.TaskRunner for more information.
    """

    @staticmethod
    def getTargetList(parsedCmd, **kwargs):
        """
        Return a list of tuples per tract, each containing (dataRefs, kwargs).

        Jointcal operates on lists of dataRefs simultaneously.
        """
        kwargs['profile_jointcal'] = parsedCmd.profile_jointcal
        kwargs['butler'] = parsedCmd.butler

        # organize data IDs by tract
        refListDict = {}
        for ref in parsedCmd.id.refList:
            refListDict.setdefault(ref.dataId["tract"], []).append(ref)
        # we call run() once with each tract
        result = [(refListDict[tract], kwargs) for tract in sorted(refListDict.keys())]
        return result

    def __call__(self, args):
        """
        @param args     Arguments for Task.run()

        @return
        - None if self.doReturnResults is False
        - A pipe.base.Struct containing these fields if self.doReturnResults is True:
            - dataRef: the provided data references, with update post-fit WCS's.
        """
        # NOTE: cannot call self.makeTask because that assumes args[0] is a single dataRef.
        dataRefList, kwargs = args
        butler = kwargs.pop('butler')
        task = self.TaskClass(config=self.config, log=self.log, butler=butler)
        result = task.run(dataRefList, **kwargs)
        if self.doReturnResults:
            return pipeBase.Struct(result=result)


class JointcalConfig(pexConfig.Config):
    """Config for jointcalTask"""

    doAstrometry = pexConfig.Field(
        doc="Fit astrometry and write the fitted result.",
        dtype=bool,
        default=True
    )
    doPhotometry = pexConfig.Field(
        doc="Fit photometry and write the fitted result.",
        dtype=bool,
        default=True
    )
    coaddName = pexConfig.Field(
        doc="Type of coadd, typically deep or goodSeeing",
        dtype=str,
        default="deep"
    )
    posError = pexConfig.Field(
        doc="Constant term for error on position (in pixel unit)",
        dtype=float,
        default=0.02,
    )
    # TODO: DM-6885 matchCut should be an afw.geom.Angle
    matchCut = pexConfig.Field(
        doc="Matching radius between fitted and reference stars (arcseconds)",
        dtype=float,
        default=3.0,
    )
    minMeasurements = pexConfig.Field(
        doc="Minimum number of associated measured stars for a fitted star to be included in the fit",
        dtype=int,
        default=2,
    )
    polyOrder = pexConfig.Field(
        doc="Polynomial order for fitting distorsion",
        dtype=int,
        default=3,
    )
    astrometryModel = pexConfig.ChoiceField(
        doc="Type of model to fit to astrometry",
        dtype=str,
        default="simplePoly",
        allowed={"simplePoly": "One polynomial per ccd",
                 "constrainedPoly": "One polynomial per ccd, and one polynomial per visit"}
    )
    photometryModel = pexConfig.ChoiceField(
        doc="Type of model to fit to photometry",
        dtype=str,
        default="simple",
        allowed={"simple": "One constant zeropoint per ccd and visit",
                 "constrained": "Constrained zeropoint per ccd, and one polynomial per visit"}
    )
    photometryVisitDegree = pexConfig.Field(
        doc="Degree of the per-visit polynomial transform for the constrained photometry model.",
        dtype=int,
        default=7,
    )
    astrometryRefObjLoader = pexConfig.ConfigurableField(
        target=LoadIndexedReferenceObjectsTask,
        doc="Reference object loader for astrometric fit",
    )
    photometryRefObjLoader = pexConfig.ConfigurableField(
        target=LoadIndexedReferenceObjectsTask,
        doc="Reference object loader for photometric fit",
    )
    sourceSelector = sourceSelectorRegistry.makeField(
        doc="How to select sources for cross-matching",
        default="astrometry"
    )

    def setDefaults(self):
        sourceSelector = self.sourceSelector["astrometry"]
        sourceSelector.setDefaults()
        # don't want to lose existing flags, just add to them.
        sourceSelector.badFlags.extend(["slot_Shape_flag"])
        # This should be used to set the FluxField value in jointcal::JointcalControl
        sourceSelector.sourceFluxType = 'Calib'


class JointcalTask(pipeBase.CmdLineTask):
    """Jointly astrometrically (photometrically later) calibrate a group of images."""

    ConfigClass = JointcalConfig
    RunnerClass = JointcalRunner
    _DefaultName = "jointcal"

    def __init__(self, butler=None, profile_jointcal=False, **kwargs):
        """
        Instantiate a JointcalTask.

        Parameters
        ----------
        butler : lsst.daf.persistence.Butler
            The butler is passed to the refObjLoader constructor in case it is
            needed. Ignored if the refObjLoader argument provides a loader directly.
            Used to initialize the astrometry and photometry refObjLoaders.
        profile_jointcal : bool
            set to True to profile different stages of this jointcal run.
        """
        pipeBase.CmdLineTask.__init__(self, **kwargs)
        self.profile_jointcal = profile_jointcal
        self.makeSubtask("sourceSelector")
        if self.config.doAstrometry:
            self.makeSubtask('astrometryRefObjLoader', butler=butler)
        if self.config.doPhotometry:
            self.makeSubtask('photometryRefObjLoader', butler=butler)

        # To hold various computed metrics for use by tests
        self.metrics = {}

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
        parser.add_argument("--profile_jointcal", default=False, action="store_true",
                            help="Profile steps of jointcal separately.")
        parser.add_id_argument("--id", "calexp", help="data ID, e.g. --id visit=6789 ccd=0..9",
                               ContainerClass=PerTractCcdDataIdContainer)
        return parser

    def _build_ccdImage(self, dataRef, associations, jointcalControl):
        """
        Extract the necessary things from this dataRef to add a new ccdImage.

        Parameters
        ----------
        dataRef : lsst.daf.persistence.ButlerDataRef
            dataRef to extract info from.
        associations : lsst.jointcal.Associations
            object to add the info to, to construct a new CcdImage
        jointcalControl : jointcal.JointcalControl
            control object for associations management

        Returns
        ------
        namedtuple
            wcs : lsst.afw.image.TanWcs
                the TAN WCS of this image, read from the calexp
            key : namedtuple
                a key to identify this dataRef by its visit and ccd ids
            filter : str
                this calexp's filter
        """
        if "visit" in dataRef.dataId.keys():
            visit = dataRef.dataId["visit"]
        else:
            visit = dataRef.getButler().queryMetadata("calexp", ("visit"), dataRef.dataId)[0]

        src = dataRef.get("src", flags=lsst.afw.table.SOURCE_IO_NO_FOOTPRINTS, immediate=True)

        visitInfo = dataRef.get('calexp_visitInfo')
        detector = dataRef.get('calexp_detector')
        ccdname = detector.getId()
        calib = dataRef.get('calexp_calib')
        tanWcs = dataRef.get('calexp_wcs')
        bbox = dataRef.get('calexp_bbox')
        filt = dataRef.get('calexp_filter')
        filterName = filt.getName()
        fluxMag0 = calib.getFluxMag0()
        photoCalib = afwImage.PhotoCalib(1.0/fluxMag0[0], fluxMag0[1]/fluxMag0[0]**2, bbox)

        goodSrc = self.sourceSelector.selectSources(src)

        if len(goodSrc.sourceCat) == 0:
            self.log.warn("no stars selected in ", visit, ccdname)
            return tanWcs
        self.log.info("%d stars selected in visit %d ccd %d", len(goodSrc.sourceCat), visit, ccdname)
        associations.addImage(goodSrc.sourceCat, tanWcs, visitInfo, bbox, filterName, photoCalib, detector,
                              visit, ccdname, jointcalControl)

        Result = collections.namedtuple('Result_from_build_CcdImage', ('wcs', 'key', 'filter'))
        Key = collections.namedtuple('Key', ('visit', 'ccd'))
        return Result(tanWcs, Key(visit, ccdname), filterName)

    @pipeBase.timeMethod
    def run(self, dataRefs, profile_jointcal=False):
        """
        Jointly calibrate the astrometry and photometry across a set of images.

        Parameters
        ----------
        dataRefs : list of lsst.daf.persistence.ButlerDataRef
            List of data references to the exposures to be fit.
        profile_jointcal : bool
            Profile the individual steps of jointcal.

        Returns
        -------
        pipe.base.Struct
            struct containing:
            * dataRefs: the provided data references that were fit (with updated WCSs)
            * oldWcsList: the original WCS from each dataRef
            * metrics: dictionary of internally-computed metrics for testing/validation.
        """
        if len(dataRefs) == 0:
            raise ValueError('Need a list of data references!')

        sourceFluxField = "slot_%sFlux" % (self.sourceSelector.config.sourceFluxType,)
        jointcalControl = lsst.jointcal.JointcalControl(sourceFluxField)
        associations = lsst.jointcal.Associations()

        visit_ccd_to_dataRef = {}
        oldWcsList = []
        filters = []
        load_cat_prof_file = 'jointcal_build_ccdImage.prof' if profile_jointcal else ''
        with pipeBase.cmdLineTask.profile(load_cat_prof_file):
            # We need the bounding-box of the focal plane for photometry visit models.
            # NOTE: we only need to read it once, because its the same for all exposures of a camera.
            camera = dataRefs[0].get('camera', immediate=True)
            self.focalPlaneBBox = camera.getFpBBox()
            for ref in dataRefs:
                result = self._build_ccdImage(ref, associations, jointcalControl)
                oldWcsList.append(result.wcs)
                visit_ccd_to_dataRef[result.key] = ref
                filters.append(result.filter)
        filters = collections.Counter(filters)

        centers = [ccdImage.getBoresightRaDec() for ccdImage in associations.getCcdImageList()]
        commonTangentPoint = lsst.afw.coord.averageCoord(centers)
        self.log.debug("Using common tangent point: %s", commonTangentPoint.getPosition())
        associations.setCommonTangentPoint(commonTangentPoint.getPosition())

        # Use external reference catalogs handled by LSST stack mechanism
        # Get the bounding box overlapping all associated images
        # ==> This is probably a bad idea to do it this way <== To be improved
        bbox = associations.getRaDecBBox()
        center = afwCoord.Coord(bbox.getCenter(), afwGeom.degrees)
        corner = afwCoord.Coord(bbox.getMax(), afwGeom.degrees)
        radius = center.angularSeparation(corner).asRadians()

        # Get astrometry_net_data path
        anDir = lsst.utils.getPackageDir('astrometry_net_data')
        if anDir is None:
            raise RuntimeError("astrometry_net_data is not setup")

        # Determine a default filter associated with the catalog. See DM-9093
        defaultFilter = filters.most_common(1)[0][0]
        self.log.debug("Using %s band for reference flux", defaultFilter)

        # TODO: need a better way to get the tract.
        tract = dataRefs[0].dataId['tract']

        if self.config.doAstrometry:
            astrometry = self._do_load_refcat_and_fit(associations, defaultFilter, center, radius,
                                                      name="Astrometry",
                                                      refObjLoader=self.astrometryRefObjLoader,
                                                      fit_function=self._fit_astrometry,
                                                      profile_jointcal=profile_jointcal,
                                                      tract=tract)
        else:
            astrometry = Astrometry(None, None, None)

        if self.config.doPhotometry:
            photometry = self._do_load_refcat_and_fit(associations, defaultFilter, center, radius,
                                                      name="Photometry",
                                                      refObjLoader=self.photometryRefObjLoader,
                                                      fit_function=self._fit_photometry,
                                                      profile_jointcal=profile_jointcal,
                                                      tract=tract,
                                                      filters=filters)
        else:
            photometry = Photometry(None, None)

        load_cat_prof_file = 'jointcal_write_results.prof' if profile_jointcal else ''
        with pipeBase.cmdLineTask.profile(load_cat_prof_file):
            self._write_results(associations, astrometry.model, photometry.model, visit_ccd_to_dataRef)

        return pipeBase.Struct(dataRefs=dataRefs, oldWcsList=oldWcsList, metrics=self.metrics)

    def _do_load_refcat_and_fit(self, associations, defaultFilter, center, radius,
                                name="", refObjLoader=None, filters=[], fit_function=None,
                                tract=None, profile_jointcal=False, match_cut=3.0):
        """Load reference catalog, perform the fit, and return the result.

        Parameters
        ----------
        associations : lsst.jointcal.Associations
            The star/reference star associations to fit.
        defaultFilter : str
            filter to load from reference catalog.
        center : lsst.afw.coord.Coord
            Center of field to load from reference catalog.
        radius : lsst.afw.geom.Angle
            On-sky radius to load from reference catalog.
        name : str
            Name of thing being fit: "Astrometry" or "Photometry".
        refObjLoader : lsst.meas.algorithms.LoadReferenceObjectsTask
            Reference object loader to load from for fit.
        filters : list of str, optional
            List of filters to load from the reference catalog.
        fit_function : function
            function to call to perform fit (takes associations object).
        tract : str
            Name of tract currently being fit.
        profile_jointcal : bool, optional
            Separately profile the fitting step.
        match_cut : float, optional
            Radius in arcseconds to find cross-catalog matches to during
            associations.associateCatalogs.

        Returns
        -------
        Result of `fit_function()`
        """
        self.log.info("====== Now processing %s...", name)
        # TODO: this should not print "trying to invert a singular transformation:"
        # if it does that, something's not right about the WCS...
        associations.associateCatalogs(match_cut)
        self.metrics['associated%sFittedStars' % name] = associations.fittedStarListSize()

        skyCircle = refObjLoader.loadSkyCircle(center,
                                               afwGeom.Angle(radius, afwGeom.radians),
                                               defaultFilter)

        # Need memory contiguity to get reference filters as a vector.
        if not skyCircle.refCat.isContiguous():
            refCat = skyCircle.refCat.copy(deep=True)
        else:
            refCat = skyCircle.refCat

        # load the reference catalog fluxes.
        # TODO: Simon will file a ticket for making this better (and making it use the color terms)
        refFluxes = {}
        refFluxErrs = {}
        for filt in filters:
            filtKeys = lsst.meas.algorithms.getRefFluxKeys(refCat.schema, filt)
            refFluxes[filt] = refCat.get(filtKeys[0])
            refFluxErrs[filt] = refCat.get(filtKeys[1])

        associations.collectRefStars(refCat, self.config.matchCut*afwGeom.arcseconds,
                                     skyCircle.fluxField, refFluxes, refFluxErrs)
        self.metrics['collected%sRefStars' % name] = associations.refStarListSize()

        associations.selectFittedStars(self.config.minMeasurements)
        self._check_star_lists(associations, name)
        self.metrics['selected%sRefStars' % name] = associations.refStarListSize()
        self.metrics['selected%sFittedStars' % name] = associations.fittedStarListSize()
        self.metrics['selected%sCcdImageList' % name] = associations.nCcdImagesValidForFit()

        load_cat_prof_file = 'jointcal_fit_%s.prof'%name if profile_jointcal else ''
        with pipeBase.cmdLineTask.profile(load_cat_prof_file):
            result = fit_function(associations)
        # TODO: this should probably be made optional and turned into a "butler save" somehow.
        # Save reference and measurement n-tuples for each tract
        tupleName = "{}_res_{}.list".format(name, tract)
        result.fit.saveResultTuples(tupleName)

        return result

    def _check_star_lists(self, associations, name):
        # TODO: these should be len(blah), but we need this properly wrapped first.
        if associations.nCcdImagesValidForFit() == 0:
            raise RuntimeError('No images in the ccdImageList!')
        if associations.fittedStarListSize() == 0:
            raise RuntimeError('No stars in the {} fittedStarList!'.format(name))
        if associations.refStarListSize() == 0:
            raise RuntimeError('No stars in the {} reference star list!'.format(name))

    def _fit_photometry(self, associations):
        """
        Fit the photometric data.

        Parameters
        ----------
        associations : lsst.jointcal.Associations
            The star/reference star associations to fit.

        Returns
        -------
        namedtuple
            fit : lsst.jointcal.PhotometryFit
                The photometric fitter used to perform the fit.
            model : lsst.jointcal.PhotometryModel
                The photometric model that was fit.
        """
        self.log.info("=== Starting photometric fitting...")

        # TODO: should use pex.config.RegistryField here (see DM-9195)
        if self.config.photometryModel == "constrained":
            model = lsst.jointcal.ConstrainedPhotometryModel(associations.getCcdImageList(),
                                                             self.focalPlaneBBox,
                                                             visitDegree=self.config.photometryVisitDegree)
        elif self.config.photometryModel == "simple":
            model = lsst.jointcal.SimplePhotometryModel(associations.getCcdImageList())

        fit = lsst.jointcal.PhotometryFit(associations, model)
        chi2 = fit.computeChi2()
        self.log.info("Initialized: %s", str(chi2))
        fit.minimize("Model")
        chi2 = fit.computeChi2()
        self.log.info(str(chi2))
        fit.minimize("Fluxes")
        chi2 = fit.computeChi2()
        self.log.info(str(chi2))
        fit.minimize("Model Fluxes")
        chi2 = fit.computeChi2()
        self.log.info("Fit prepared with %s", str(chi2))

        chi2 = self._iterate_fit(fit, model, 20, "photometry", "Model Fluxes")

        self.metrics['photometryFinalChi2'] = chi2.chi2
        self.metrics['photometryFinalNdof'] = chi2.ndof
        return Photometry(fit, model)

    def _fit_astrometry(self, associations):
        """
        Fit the astrometric data.

        Parameters
        ----------
        associations : lsst.jointcal.Associations
            The star/reference star associations to fit.

        Returns
        -------
        namedtuple
            fit : lsst.jointcal.AstrometryFit
                The astrometric fitter used to perform the fit.
            model : lsst.jointcal.AstrometryModel
                The astrometric model that was fit.
            sky_to_tan_projection : lsst.jointcal.ProjectionHandler
                The model for the sky to tangent plane projection that was used in the fit.
        """

        self.log.info("=== Starting astrometric fitting...")

        associations.deprojectFittedStars()

        # NOTE: need to return sky_to_tan_projection so that it doesn't get garbage collected.
        # TODO: could we package sky_to_tan_projection and model together so we don't have to manage
        # them so carefully?
        sky_to_tan_projection = lsst.jointcal.OneTPPerVisitHandler(associations.getCcdImageList())

        if self.config.astrometryModel == "constrainedPoly":
            model = lsst.jointcal.ConstrainedPolyModel(associations.getCcdImageList(),
                                                       sky_to_tan_projection, True, 0)
        elif self.config.astrometryModel == "simplePoly":
            model = lsst.jointcal.SimplePolyModel(associations.getCcdImageList(),
                                                  sky_to_tan_projection,
                                                  True, 0, self.config.polyOrder)

        fit = lsst.jointcal.AstrometryFit(associations, model, self.config.posError)
        chi2 = fit.computeChi2()
        self.log.info("Initialized: %s", str(chi2))
        fit.minimize("Distortions")
        chi2 = fit.computeChi2()
        self.log.info(str(chi2))
        fit.minimize("Positions")
        chi2 = fit.computeChi2()
        self.log.info(str(chi2))
        fit.minimize("Distortions Positions")
        chi2 = fit.computeChi2()
        self.log.info(str(chi2))

        chi2 = self._iterate_fit(fit, model, 20, "astrometry", "Distortions Positions")

        self.metrics['astrometryFinalChi2'] = chi2.chi2
        self.metrics['astrometryFinalNdof'] = chi2.ndof

        return Astrometry(fit, model, sky_to_tan_projection)

    def _iterate_fit(self, fit, model, max_steps, name, whatToFit):
        """Run fit.minimize up to max_steps times, returning the final chi2."""

        for i in range(max_steps):
            r = fit.minimize(whatToFit, 5)  # outlier removal at 5 sigma.
            chi2 = fit.computeChi2()
            self.log.info(str(chi2))
            if r == MinimizeResult.Converged:
                self.log.debug("fit has converged - no more outliers - redo minimixation"
                               "one more time in case we have lost accuracy in rank update")
                # Redo minimization one more time in case we have lost accuracy in rank update
                r = fit.minimize(whatToFit, 5)  # outliers removal at 5 sigma.
                chi2 = fit.computeChi2()
                self.log.info("Fit completed with: %s", str(chi2))
                break
            elif r == MinimizeResult.Failed:
                self.log.warn("minimization failed")
                break
            elif r == MinimizeResult.Chi2Increased:
                self.log.warn("still some ouliers but chi2 increases - retry")
            else:
                self.log.error("unxepected return code from minimize")
                break
        else:
            self.log.error("%s failed to converge after %d steps"%(name, max_steps))

        return chi2

    def _write_results(self, associations, astrometry_model, photometry_model, visit_ccd_to_dataRef):
        """
        Write the fitted results (photometric and astrometric) to a new 'wcs' dataRef.

        Parameters
        ----------
        associations : lsst.jointcal.Associations
            The star/reference star associations to fit.
        astrometry_model : lsst.jointcal.AstrometryModel
            The astrometric model that was fit.
        photometry_model : lsst.jointcal.PhotometryModel
            The photometric model that was fit.
        visit_ccd_to_dataRef : dict of Key: lsst.daf.persistence.ButlerDataRef
            dict of ccdImage identifiers to dataRefs that were fit
        """

        ccdImageList = associations.getCcdImageList()
        for ccdImage in ccdImageList:
            # TODO: there must be a better way to identify this ccdImage than a visit,ccd pair?
            ccd = ccdImage.ccdId
            visit = ccdImage.visit
            dataRef = visit_ccd_to_dataRef[(visit, ccd)]
            exp = afwImage.ExposureI(0, 0)
            if self.config.doAstrometry:
                self.log.info("Updating WCS for visit: %d, ccd: %d", visit, ccd)
                tanSip = astrometry_model.produceSipWcs(ccdImage)
                tanWcs = lsst.jointcal.gtransfoToTanWcs(tanSip, ccdImage.imageFrame, False)
                exp.setWcs(tanWcs)
                try:
                    dataRef.put(exp, 'wcs')
                except pexExceptions.Exception as e:
                    self.log.fatal('Failed to write updated Wcs: %s', str(e))
                    raise e
            if self.config.doPhotometry:
                self.log.info("Updating PhotoCalib for visit: %d, ccd: %d", visit, ccd)
                photoCalib = photometry_model.toPhotoCalib(ccdImage)
                try:
                    dataRef.put(photoCalib, 'photoCalib')
                except pexExceptions.Exception as e:
                    self.log.fatal('Failed to write updated PhotoCalib: %s', str(e))
                    raise e
