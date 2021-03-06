// -*- LSST-C++ -*-
#ifndef LSST_JOINTCAL_LIST_MATCH_H
#define LSST_JOINTCAL_LIST_MATCH_H

#include <string>

#include "lsst/jointcal/BaseStar.h"
#include "lsst/jointcal/StarMatch.h"

namespace lsst {
namespace jointcal {

class Gtransfo;
class GtransfoLin;

//! Parameters to be provided to combinatorial searches
struct MatchConditions {
    int nStarsList1, nStarsList2;
    int maxTrialCount;
    double nSigmas;
    double maxShiftX, maxShiftY;
    double sizeRatio, deltaSizeRatio, minMatchRatio;
    int printLevel;
    int algorithm;

    MatchConditions()
            : nStarsList1(70),
              nStarsList2(70),
              maxTrialCount(4),
              nSigmas(3.),
              maxShiftX(50),
              maxShiftY(50),
              sizeRatio(1),
              deltaSizeRatio(0.1 * sizeRatio),
              minMatchRatio(1. / 3.),
              printLevel(0),
              algorithm(2) {}

    double minSizeRatio() const { return sizeRatio - deltaSizeRatio; }
    double maxSizeRatio() const { return sizeRatio + deltaSizeRatio; }
};

/*! \file
    \brief Combinatorial searches for linear transformations to go from
           list1 to list2.

    The following routines search a geometrical transformation that make
two lists of stars to match geometrically as well as possible. They are used
either to match two images of the same sky area, or an image with a catalogue.
They assume that fluxes assigned to stars are actual fluxes, i.e. the brighter
the star, the higher the flux. They however only rely on flux ordering,
not values.
 */

//! searches a geometrical transformation that goes from list1 to list2.
/*!  The found transformation is a field of the returned object, as well as the star pairs
(the matches) that were constructed.  (see StarMatchList class definition for more details).
The various cuts are contained in conditions (see listmatch.h) for its contents.
This routine searches a transformation that involves a shift and a rotation. */

std::unique_ptr<StarMatchList> matchSearchRotShift(BaseStarList &list1, BaseStarList &list2,
                                                   const MatchConditions &conditions);

//! same as above but searches also a flipped solution.

std::unique_ptr<StarMatchList> matchSearchRotShiftFlip(BaseStarList &list1, BaseStarList &list2,
                                                       const MatchConditions &conditions);

//! assembles star matches.
/*! It picks stars in list1, transforms them through guess, and collects
closest star in list2, and builds a match if closer than maxDist). */

std::unique_ptr<StarMatchList> listMatchCollect(const BaseStarList &list1, const BaseStarList &list2,
                                                const Gtransfo *guess, const double maxDist);

//! same as before except that the transfo is the identity

std::unique_ptr<StarMatchList> listMatchCollect(const BaseStarList &list1, const BaseStarList &list2,
                                                const double maxDist);

//! searches for a 2 dimensional shift using a very crude histogram method.

std::unique_ptr<GtransfoLin> listMatchupShift(const BaseStarList &list1, const BaseStarList &list2,
                                              const Gtransfo &gtransfo, double maxShift, double binSize = 0);

std::unique_ptr<Gtransfo> listMatchCombinatorial(const BaseStarList &list1, const BaseStarList &list2,
                                                 const MatchConditions &conditions = MatchConditions());
std::unique_ptr<Gtransfo> listMatchRefine(const BaseStarList &list1, const BaseStarList &list2,
                                          std::unique_ptr<Gtransfo> transfo, const int maxOrder = 3);

#ifdef DO_WE_NEED_THAT
inline Gtransfo *ListMatch(const BaseStarList &list1, const BaseStarList &list2, const int maxOrder = 3) {
    Gtransfo *transfo = listMatchCombinatorial(list1, list2);
    transfo = listMatchRefine(list1, list2, transfo, maxOrder);
    return transfo;
}
#endif /*  DO_WE_NEED_THAT */
}  // namespace jointcal
}  // namespace lsst

#endif  // LSST_JOINTCAL_LIST_MATCH_H
