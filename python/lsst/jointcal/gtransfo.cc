/*
 * LSST Data Management System
 *
 * This product includes software developed by the
 * LSST Project (http://www.lsst.org/).
 * See the COPYRIGHT file
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the LSST License Statement and
 * the GNU General Public License along with this program.  If not,
 * see <https://www.lsstcorp.org/LegalNotices/>.
 */

#include "astshim.h"
#include "numpy/arrayobject.h"
#include "pybind11/pybind11.h"
#include "ndarray/pybind11.h"
#include "ndarray/eigen.h"
#include "Eigen/Core"

#include "lsst/utils/python.h"

#include "lsst/jointcal/Frame.h"
#include "lsst/jointcal/Gtransfo.h"
#include "lsst/jointcal/Point.h"

namespace py = pybind11;
using namespace pybind11::literals;

namespace lsst {
namespace jointcal {
namespace {

void declareGtransfo(py::module &mod) {
    py::class_<Gtransfo, std::shared_ptr<Gtransfo>, std::unique_ptr<Gtransfo>> cls(mod, "Gtransfo");

    cls.def("apply", (jointcal::Point(Gtransfo::*)(const jointcal::Point &) const) & Gtransfo::apply,
            "inPos"_a);
    cls.def("apply", (jointcal::Frame(Gtransfo::*)(Frame const &, bool) const) & Gtransfo::apply,
            "inputframe"_a, "inscribed"_a);
    cls.def("getNpar", &GtransfoPoly::getNpar);
    cls.def("offsetParams", &GtransfoPoly::offsetParams);
    cls.def("toAstMap", &GtransfoPoly::toAstMap);

    utils::python::addOutputOp(cls, "__str__");
}

void declareGtransfoIdentity(py::module &mod) {
    py::class_<GtransfoIdentity, std::shared_ptr<GtransfoIdentity>, Gtransfo> cls(mod, "GtransfoIdentity");
}

void declareGtransfoPoly(py::module &mod) {
    py::class_<GtransfoPoly, std::shared_ptr<GtransfoPoly>, Gtransfo> cls(mod, "GtransfoPoly");

    cls.def(py::init<const unsigned>(), "order"_a);
    cls.def("getOrder", &GtransfoPoly::getOrder);
    cls.def("coeff", (double (GtransfoPoly::*)(unsigned const, unsigned const, unsigned const) const) &
                             GtransfoPoly::coeff);
    cls.def("write", [](GtransfoPoly const &self) {
        std::stringstream result;
        self.write(result);
        return result.str();
    });
    cls.def("read", [](GtransfoPoly &self, std::string const &str) {
        std::istringstream istr(str);
        self.read(istr);
    });
}

void declareGtransfoLin(py::module &mod) {
    py::class_<GtransfoLin, std::shared_ptr<GtransfoLin>, GtransfoPoly> cls(mod, "GtransfoLin");
}

void declareGtransfoLinShift(py::module &mod) {
    py::class_<GtransfoLinShift, std::shared_ptr<GtransfoLinShift>, GtransfoLin> cls(mod, "GtransfoLinShift");
}

void declareGtransfoLinRot(py::module &mod) {
    py::class_<GtransfoLinRot, std::shared_ptr<GtransfoLinRot>, GtransfoLin> cls(mod, "GtransfoLinRot");
}

void declareGtransfoLinScale(py::module &mod) {
    py::class_<GtransfoLinScale, std::shared_ptr<GtransfoLinScale>, GtransfoLin> cls(mod, "GtransfoLinScale");
}

void declareGtransfoSkyWcs(py::module &mod) {
    py::class_<GtransfoSkyWcs, std::shared_ptr<GtransfoSkyWcs>, Gtransfo> cls(mod, "GtransfoSkyWcs");
    cls.def("getSkyWcs", &GtransfoSkyWcs::getSkyWcs);
}

void declareBaseTanWcs(py::module &mod) {
    py::class_<BaseTanWcs, std::shared_ptr<BaseTanWcs>, Gtransfo> cls(mod, "BaseTanWcs");
}

void declareTanPix2RaDec(py::module &mod) {
    py::class_<TanPix2RaDec, std::shared_ptr<TanPix2RaDec>, Gtransfo> cls(mod, "TanPix2RaDec");
}

void declareTanRaDec2Pix(py::module &mod) {
    py::class_<TanRaDec2Pix, std::shared_ptr<TanRaDec2Pix>, Gtransfo> cls(mod, "TanRaDec2Pix");
}

void declareTanSipPix2RaDec(py::module &mod) {
    py::class_<TanSipPix2RaDec, std::shared_ptr<TanSipPix2RaDec>, BaseTanWcs> cls(mod, "TanSipPix2RaDec");
}

PYBIND11_PLUGIN(gtransfo) {
    py::module::import("astshim.mapping");
    py::module::import("lsst.jointcal.frame");
    py::module::import("lsst.jointcal.star");
    py::module mod("gtransfo");

    if (_import_array() < 0) {
        PyErr_SetString(PyExc_ImportError, "numpy.core.multiarray failed to import");
        return nullptr;
    }

    declareGtransfo(mod);
    declareGtransfoIdentity(mod);
    declareGtransfoPoly(mod);
    declareGtransfoLin(mod);
    declareGtransfoLinShift(mod);
    declareGtransfoLinRot(mod);
    declareGtransfoLinScale(mod);
    declareGtransfoSkyWcs(mod);
    declareBaseTanWcs(mod);
    declareTanPix2RaDec(mod);
    declareTanRaDec2Pix(mod);
    declareTanSipPix2RaDec(mod);

    // utility functions
    mod.def("inversePolyTransfo", &inversePolyTransfo, "forward"_a, "domain"_a, "precision"_a,
            "maxOrder"_a = 9, "nSteps"_a = 50);

    return mod.ptr();
}
}  // namespace
}  // namespace jointcal
}  // namespace lsst
