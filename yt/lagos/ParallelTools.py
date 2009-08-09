"""
Parallel data mapping techniques for yt

Author: Matthew Turk <matthewturk@gmail.com>
Affiliation: KIPAC/SLAC/Stanford
Homepage: http://yt.enzotools.org/
License:
  Copyright (C) 2008-2009 Matthew Turk.  All Rights Reserved.

  This file is part of yt.

  yt is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from yt.lagos import *
from yt.funcs import *
import yt.logger, logging
import itertools, sys, cStringIO

if os.path.basename(sys.executable) in \
        ["mpi4py", "embed_enzo",
         "python"+sys.version[:3]+"-mpi"] \
    or "--parallel" in sys.argv or '_parallel' in dir(sys) \
    or any(["ipengine" in arg for arg in sys.argv]):
    from mpi4py import MPI
    parallel_capable = (MPI.COMM_WORLD.size > 1)
    if parallel_capable:
        mylog.info("Parallel computation enabled: %s / %s",
                   MPI.COMM_WORLD.rank, MPI.COMM_WORLD.size)
        ytcfg["yt","__parallel_rank"] = str(MPI.COMM_WORLD.rank)
        ytcfg["yt","__parallel_size"] = str(MPI.COMM_WORLD.size)
        ytcfg["yt","__parallel"] = "True"
        # I believe we do not need to turn this off manually
        #ytcfg["yt","StoreParameterFiles"] = "False"
        # Now let's make sure we have the right options set.
        if MPI.COMM_WORLD.rank > 0:
            if ytcfg.getboolean("lagos","serialize"):
                ytcfg["lagos","onlydeserialize"] = "True"
            if ytcfg.getboolean("yt","LogFile"):
                ytcfg["yt","LogFile"] = "False"
                yt.logger.disable_file_logging()
        f = logging.Formatter("P%03i %s" % (MPI.COMM_WORLD.rank,
                                            yt.logger.fstring))
        yt.logger.rootLogger.handlers[0].setFormatter(f)
else:
    parallel_capable = False

class ObjectIterator(object):
    def __init__(self, pobj, just_list = False, attr='_grids'):
        self.pobj = pobj
        if hasattr(pobj, attr) and getattr(pobj, attr) is not None:
            gs = getattr(pobj, attr)
        else:
            gs = getattr(pobj._data_source, attr)
        if hasattr(gs[0], 'proc_num'):
            # This one sort of knows about MPI, but not quite
            self._objs = [g for g in gs if g.proc_num ==
                          ytcfg.getint('yt','__parallel_rank')]
            self._use_all = True
        else:
            self._objs = gs
            if hasattr(self._objs[0], 'filename'):
                self._objs = sorted(self._objs, key = lambda g: g.filename)
            self._use_all = False
        self.ng = len(self._objs)
        self.just_list = just_list

    def __iter__(self):
        for obj in self._objs: yield obj
        
class ParallelObjectIterator(ObjectIterator):
    """
    This takes an object, pobj, that implements ParallelAnalysisInterface,
    and then does its thing.
    """
    def __init__(self, pobj, just_list = False, attr='_grids',
                 round_robin=False):
        ObjectIterator.__init__(self, pobj, just_list, attr=attr)
        self._offset = MPI.COMM_WORLD.rank
        self._skip = MPI.COMM_WORLD.size
        # Note that we're doing this in advance, and with a simple means
        # of choosing them; more advanced methods will be explored later.
        if self._use_all:
            self.my_obj_ids = na.arange(len(self._objs))
        else:
            if not round_robin:
                self.my_obj_ids = na.array_split(
                                na.arange(len(self._objs)), self._skip)[self._offset]
            else:
                self.my_obj_ids = na.arange(len(self._objs))[self._offset::self._skip]
        
    def __iter__(self):
        for gid in self.my_obj_ids:
            yield self._objs[gid]
        if not self.just_list: self.pobj._finalize_parallel()

def parallel_simple_proxy(func):
    if not parallel_capable: return func
    @wraps(func)
    def single_proc_results(self, *args, **kwargs):
        retval = None
        if self._processing or not self._distributed:
            return func(self, *args, **kwargs)
        if self._owner == MPI.COMM_WORLD.rank:
            self._processing = True
            retval = func(self, *args, **kwargs)
            self._processing = False
        retval = MPI.COMM_WORLD.bcast(retval, root=self._owner)
        MPI.COMM_WORLD.Barrier()
        return retval
    return single_proc_results

class ParallelDummy(type):
    # All attributes that don't start with _ get replaced with
    # parallel_simple_proxy attributes.
    def __init__(cls, name, bases, d):
        super(ParallelDummy, cls).__init__(name, bases, d)
        skip = d.pop("dont_wrap", [])
        extra = d.pop("extra_wrap", [])
        for attrname in d:
            if attrname.startswith("_") or attrname in skip:
                if attrname not in extra: continue
            attr = getattr(cls, attrname)
            if type(attr) == types.MethodType:
                setattr(cls, attrname, parallel_simple_proxy(attr))

def parallel_passthrough(func):
    @wraps(func)
    def passage(self, data):
        if not self._distributed: return data
        return func(self, data)
    return passage

def parallel_blocking_call(func):
    @wraps(func)
    def barrierize(*args, **kwargs):
        mylog.debug("Entering barrier before %s", func.func_name)
        MPI.COMM_WORLD.Barrier()
        retval = func(*args, **kwargs)
        mylog.debug("Entering barrier after %s", func.func_name)
        MPI.COMM_WORLD.Barrier()
        return retval
    if parallel_capable:
        return barrierize
    else:
        return func

def parallel_splitter(f1, f2):
    @wraps(f1)
    def in_order(*args, **kwargs):
        MPI.COMM_WORLD.Barrier()
        if MPI.COMM_WORLD.rank == 0:
            f1(*args, **kwargs)
        MPI.COMM_WORLD.Barrier()
        if MPI.COMM_WORLD.rank != 0:
            f2(*args, **kwargs)
        MPI.COMM_WORLD.Barrier()
    if not parallel_capable: return f1
    return in_order

def parallel_root_only(func):
    @wraps(func)
    def root_only(*args, **kwargs):
        if MPI.COMM_WORLD.rank == 0:
            try:
                func(*args, **kwargs)
                all_clear = 1
            except:
                traceback.print_last()
                all_clear = 0
        else:
            all_clear = None
        MPI.COMM_WORLD.Barrier()
        all_clear = MPI.COMM_WORLD.bcast(all_clear, root=0)
        if not all_clear: raise RuntimeError
    if parallel_capable: return root_only
    return func

class ParallelAnalysisInterface(object):
    _grids = None
    _distributed = parallel_capable

    def _get_objs(self, attr, *args, **kwargs):
        if self._distributed:
            rr = kwargs.pop("round_robin", False)
            self._initialize_parallel(*args, **kwargs)
            return ParallelObjectIterator(self, attr=attr,
                    round_robin=rr)
        return ObjectIterator(self, attr=attr)

    def _get_grids(self, *args, **kwargs):
        if self._distributed:
            self._initialize_parallel(*args, **kwargs)
            return ParallelObjectIterator(self, attr='_grids')
        return ObjectIterator(self, attr='_grids')

    def _get_grid_objs(self):
        if self._distributed:
            return ParallelObjectIterator(self, True, attr='_grids')
        return ObjectIterator(self, True, attr='_grids')

    def _initialize_parallel(self):
        pass

    def _finalize_parallel(self):
        pass

    def _partition_hierarchy_2d(self, axis):
        if not self._distributed:
           return False, self.hierarchy.grid_collection(self.center, self.hierarchy.grids)

        xax, yax = x_dict[axis], y_dict[axis]
        cc = MPI.Compute_dims(MPI.COMM_WORLD.size, 2)
        mi = MPI.COMM_WORLD.rank
        cx, cy = na.unravel_index(mi, cc)
        x = na.mgrid[0:1:(cc[0]+1)*1j][cx:cx+2]
        y = na.mgrid[0:1:(cc[1]+1)*1j][cy:cy+2]

        DLE, DRE = self.pf["DomainLeftEdge"], self.pf["DomainRightEdge"]
        LE = na.ones(3, dtype='float64') * DLE
        RE = na.ones(3, dtype='float64') * DRE
        LE[xax] = x[0] * (DRE[xax]-DLE[xax]) + DLE[xax]
        RE[xax] = x[1] * (DRE[xax]-DLE[xax]) + DLE[xax]
        LE[yax] = y[0] * (DRE[yax]-DLE[yax]) + DLE[yax]
        RE[yax] = y[1] * (DRE[yax]-DLE[yax]) + DLE[yax]
        mylog.debug("Dimensions: %s %s", LE, RE)

        reg = self.hierarchy.region_strict(self.center, LE, RE)
        return True, reg

    def _partition_hierarchy_3d(self, padding=0.0):
        LE, RE = self.pf["DomainLeftEdge"], self.pf["DomainRightEdge"]
        if not self._distributed:
           return False, LE, RE, self.hierarchy.grid_collection(self.center, self.hierarchy.grids)

        cc = MPI.Compute_dims(MPI.COMM_WORLD.size, 3)
        mi = MPI.COMM_WORLD.rank
        cx, cy, cz = na.unravel_index(mi, cc)
        x = na.mgrid[LE[0]:RE[0]:(cc[0]+1)*1j][cx:cx+2]
        y = na.mgrid[LE[1]:RE[1]:(cc[1]+1)*1j][cy:cy+2]
        z = na.mgrid[LE[2]:RE[2]:(cc[2]+1)*1j][cz:cz+2]

        LE = na.array([x[0], y[0], z[0]], dtype='float64')
        RE = na.array([x[1], y[1], z[1]], dtype='float64')

        if padding > 0:
            return True, \
                LE, RE, self.hierarchy.periodic_region_strict(self.center, LE-padding, RE+padding)

        return False, LE, RE, self.hierarchy.region_strict(self.center, LE, RE)

    def _partition_hierarchy_3d_bisection(self, axis, bins, counts, LE = None, RE = None,\
        old_group = None, old_comm = None):
        """
        Partition the volume into evenly weighted subvolumes using the distribution
        in counts. The bisection happens in the MPI communicator group old_group.
        """
        if not self._distributed:
           LE, RE = self.pf["DomainLeftEdge"], self.pf["DomainRightEdge"]
           return False, LE, RE, self.hierarchy.grid_collection(self.center, self.hierarchy.grids)
        
        # First time through the world is the current group.
        if old_group == None or old_comm == None:
            old_group = MPI.COMM_WORLD.Get_group()
            old_comm = MPI.COMM_WORLD

        ra = old_group.Get_rank() # In this group, not WORLD, unless it's the first time.
        
        # First find the total number of particles in my group.
        parts = old_group.allreduce(int(counts.sum()), op=MPI.SUM)
        # Now the full sum in the bins along this axis in this group.
        full_counts = na.empty(counts.size, dtype='int64')
        old_group.Allreduce([counts, MPI.INT], [full_counts, MPI.INT], op=MPI.SUM)
        # Find the bin that passes the midpoint.
        sum = 0
        bin = 0
        while sum < (parts/2):
            lastsum = sum
            sum += full_counts[bin]
            bin += 1
        # Bin edges
        left_edge = bins[bin-1]
        right_edge = bins[bin]
        # Find a better approx of the midpoint cut line using y = ax + b, then
        # solve for x in 0.5 = ax + b
        a = float(sum - lastsum) / (right_edge - left_edge)
        b = float(lastsum) - left_edge * a
        midpoint = (0.5 - b) / a
        # I have some worries I've missed something.
        if midpoint < left_edge or midpoint > right_edge:
            print 'stupid, fix midpoint!'
            sys.exit()
        
        # Now we need to split the members of this group into a top and bottom
        # half. The values that go into the _ranks are the ranks of the tasks
        # in *this* communicator group, which go zero to size - 1. They are not
        # the same as the global ranks!
        old_group_size = old_group.Get_size()
        top = na.arange(old_group_size/2)
        top_ranks = [ (top[0], top[-1], 1), ] # [ (start, stop, step), ]
        bot = na.arange(old_group_size/2, old_group_size)
        bot_ranks = [ (bot[0], bot[-1], 1), ]
        
        # Based on where we are, adjust our LE or RE, depending on axis. At the
        # same time assign the new MPI group membership.
        if ra in top:
            # Adjust our LE.
            LE[axis] = midpoint
            new_group = old_group.Range_incl(top_ranks)
            new_comm = old_comm.Create(new_group)
        else:
            # Adjust our RE.
            RE[axis] = midpoint
            new_group = old_group.Range_incl(bot_ranks)
            new_comm = old_comm.Create(new_group)
        
        # Return a new subvolume and associated stuff.
        return new_group, new_comm, LE, RE, self.hierarchy.region_strict(self.center, LE, RE)

    def _partition_hierarchy_3d_weighted_1d(self, weight=None, bins=None, padding=0.0, axis=0, min_sep=.1):
        LE, RE = self.pf["DomainLeftEdge"], self.pf["DomainRightEdge"]
        if not self._distributed:
           return False, LE, RE, self.hierarchy.grid_collection(self.center, self.hierarchy.grids)

        cc = MPI.Compute_dims(MPI.COMM_WORLD.size, 3)
        mi = MPI.COMM_WORLD.rank
        si = MPI.COMM_WORLD.size
        cx, cy, cz = na.unravel_index(mi, cc)

        gridx = na.mgrid[LE[0]:RE[0]:(cc[0]+1)*1j]
        gridy = na.mgrid[LE[1]:RE[1]:(cc[1]+1)*1j]
        gridz = na.mgrid[LE[2]:RE[2]:(cc[2]+1)*1j]

        x = gridx[cx:cx+2]
        y = gridy[cy:cy+2]
        z = gridz[cz:cz+2]

        LE = na.array([x[0], y[0], z[0]], dtype='float64')
        RE = na.array([x[1], y[1], z[1]], dtype='float64')

        # Default to normal if we don't have a weight, or our subdivisions are
        # not enough to warrant this procedure.
        if weight is None or cc[axis] < 1:
            if padding > 0:
                return True, \
                    LE, RE, self.hierarchy.periodic_region_strict(self.center, LE-padding, RE+padding)

            return False, LE, RE, self.hierarchy.region_strict(self.center, LE, RE)

        # Find the densest subvolumes globally
        local_weight = na.zeros((si, weight.size),dtype='float64')
        local_weight[mi,:] = weight
        weights = self._mpi_allsum(local_weight)
        avg_weight = weights.mean()
        weights = weights.max(axis=0)
        
        moved_count = 0
        moved = {}
        w_copy = weights.copy()
        
        if mi == 0:
            print 'w_copy',w_copy,'gridx',gridx
        
        while moved_count < (cc[axis]-1):
            con = False
            # Find the current peak
            hi_mark = na.argmax(w_copy)
            # If this peak isn't high enough, we're done
            height = w_copy[hi_mark]
            if height < 10.*avg_weight:
                if mi == 0:
                    print 'breaking',moved_count, height, avg_weight
                break
            # If this mark is too close to a previous one, avg this one out
            # and restart a search.
            new_cen = (bins[hi_mark] + bins[hi_mark+1])/2.
            if mi==0:
                print 'moved',moved
            for source in moved:
                if mi == 0:
                    print 'boobies',abs(moved[source] - new_cen)
                if abs(moved[source] - new_cen) < min_sep:
                    w_copy[hi_mark] = avg_weight
                    if mi == 0:
                        print 'continued'
                    con = True
            if con:
                continue
            # Find the lowest value entry
            lo_mark = na.argmin(w_copy)
            # Record this as a new mapping.
            moved[(bins[lo_mark] + bins[lo_mark+1])/2.] = (bins[hi_mark] + bins[hi_mark+1])/2.
            # Fix the values so they're not pulled again.
            w_copy[hi_mark] = avg_weight
            w_copy[lo_mark] = avg_weight
            moved_count += 1
        
        # Now for each key in moved, we move the axis closest to that value to
        # the value in the dict.
        temp_gridx = []
        for source in moved:
            tomove = na.argmin(abs(gridx - source))
            temp_gridx.append(moved[source])
            gridx[tomove] = -1.
        
        for g in gridx:
            if g >= 0.:
                temp_gridx.append(g)
        
        temp_gridx.sort()
        gridx = na.array(temp_gridx)
        if mi == 0:
            print 'gridx',gridx,'len=',len(gridx)
        x = gridx[cx:cx+2]
        y = gridy[cy:cy+2]
        z = gridz[cz:cz+2]

        LE = na.array([x[0], y[0], z[0]], dtype='float64')
        RE = na.array([x[1], y[1], z[1]], dtype='float64')

        if padding > 0:
            return True, \
                LE, RE, self.hierarchy.periodic_region_strict(self.center, LE-padding, RE+padding)

        return False, LE, RE, self.hierarchy.region_strict(self.center, LE, RE)



    def _partition_hierarchy_3d_weighted(self, weight=None, padding=0.0, agg=8.):
        LE, RE = self.pf["DomainLeftEdge"], self.pf["DomainRightEdge"]
        if not self._distributed:
           return False, LE, RE, self.hierarchy.grid_collection(self.center, self.hierarchy.grids)

        cc = MPI.Compute_dims(MPI.COMM_WORLD.size, 3)
        mi = MPI.COMM_WORLD.rank
        cx, cy, cz = na.unravel_index(mi, cc)

        gridx = na.mgrid[LE[0]:RE[0]:(cc[0]+1)*1j]
        gridy = na.mgrid[LE[1]:RE[1]:(cc[1]+1)*1j]
        gridz = na.mgrid[LE[2]:RE[2]:(cc[2]+1)*1j]

        x = gridx[cx:cx+2]
        y = gridy[cy:cy+2]
        z = gridz[cz:cz+2]

        LE = na.array([x[0], y[0], z[0]], dtype='float64')
        RE = na.array([x[1], y[1], z[1]], dtype='float64')
        
        old_vol = ((RE - LE)**2).sum()

        # Default to normal if we don't have a weight, or our subdivisions are
        # not enough to warrant this procedure.
        if weight is None or cc[0] < 2 or cc[1] < 2 or cc[2] < 2:
            if padding > 0:
                return True, \
                    LE, RE, self.hierarchy.periodic_region_strict(self.center, LE-padding, RE+padding)

            return False, LE, RE, self.hierarchy.region_strict(self.center, LE, RE)

        # Build the matrix of weights.
        weights = na.zeros(cc, dtype='float64')
        weights[cx,cy,cz] = weight
        weights = self._mpi_allsum(weights)
        weights = weights / weights.sum()

        # Figure out the sums of weights along the axes
        xface = weights.sum(axis=0)
        yface = weights.sum(axis=1)
        zface = weights.sum(axis=2)
        
        xedge = yface.sum(axis=1)
        yedge = xface.sum(axis=1)
        zedge = xface.sum(axis=0)

        # Get a polynomial fit to each axis weight distribution
        xcen = gridx[:-1]
        xcen += xcen[1]/2.
        ycen = gridy[:-1]
        ycen += ycen[1]/2.
        zcen = gridz[:-1]
        zcen += zcen[1]/2.

        xfit = na.polyfit(xcen, xedge, 3)
        yfit = na.polyfit(ycen, yedge, 3)
        zfit = na.polyfit(zcen, zedge, 3)
        
        # Find the normalized weights with trapizoidal integration
        # We also apply an aggression factor to the values to make the
        # boundaries shift more.
        div_count = int(1. / padding)
        divs = na.arange(div_count+1, dtype='float64') / div_count
        xvals = na.polyval(xfit, divs)
        for i,xv in enumerate(xvals):
            if xv > xedge.mean(): xvals[i] *= agg
        yvals = na.polyval(yfit, divs)
        for i,yv in enumerate(yvals):
            if yv > yedge.mean(): yvals[i] *= agg
        zvals = na.polyval(zfit, divs)
        for i,zv in enumerate(zvals):
            if zv > zedge.mean(): zvals[i] *= agg
        xnorm = na.trapz(xvals, x=divs)
        ynorm = na.trapz(yvals, x=divs)
        znorm = na.trapz(zvals, x=divs)

        # We want to start the integration from the side of the axis where
        # the highest density is, so that it gets small regions.
        xstart = float(na.argmax(xedge))/2.
        if xstart > 0.5: xstart = div_count
        else: xstart = 0
        
        ystart = float(na.argmax(yedge))/2.
        if ystart > 0.5: ystart = div_count
        else: ystart = 0
        
        zstart = float(na.argmax(zedge))/2.
        if zstart > 0.5: zstart = div_count
        else: zstart = 0

        
        # Find the boundaries. We are assured that none of the boundaries are
        # too small because each step of div is big enough because it's set
        # by the padding.
        nextx = 1./xedge.size
        nexty = 1./yedge.size
        nextz = 1./zedge.size
        boundx = [0.]
        boundy = [0.]
        boundz = [0.]
        donex, doney, donez = False, False, False
        for i in xrange(div_count):
            if xstart == 0:
                xi = 0
                xv = i
                xa = i
            else:
                xi = div_count - i
                xf = div_count
                xa = xi
            if (na.trapz(xvals[xi:xf], x=divs[xi:xf])/xnorm) >= nextx and not donex:
                boundx.append(divs[xa])
                if len(boundx) == cc[0]:
                    donex = True
                nextx += 1./xedge.size
            if ystart == 0:
                yi = 0
                yf = i
                ya = i
            else:
                yi = div_count - i
                yf = div_count
                ya = yi
            if (na.trapz(yvals[yi:yf], x=divs[yi:yf])/ynorm) >= nexty and not doney:
                boundy.append(divs[ya])
                if len(boundy) == cc[1]:
                    doney = True
                nexty += 1./yedge.size
            if zstart == 0:
                zi = 0
                zf = i
                za = i
            else:
                zi = div_count - i
                zf = div_count
                za = zi
            if (na.trapz(zvals[zi:zf], x=divs[zi:zf])/znorm) >= nextz and not donez:
                boundz.append(divs[za])
                if len(boundz) == cc[2]:
                    donez = True
                nextz += 1./zedge.size
        
        boundx.sort()
        boundy.sort()
        boundz.sort()
        
        # Check for problems, fatally for now because I'm the only one using this
        # and I don't mind that, it will help me fix things.
        if len(boundx) < cc[0] or len(boundy) < cc[1] or len(boundz) < cc[2]:
            print 'weighted stuff broken.'
            print 'cc', cc
            print len(boundx), len(boundy), len(boundz)
            sys.exit()
        
        boundx.append(1.)
        boundy.append(1.)
        boundz.append(1.)
        
        if mi == 0:
           print 'x',boundx
           print 'y',boundy
           print 'z',boundz
        
        # Update the boundaries
        new_LE = na.array([boundx[cx], boundy[cy], boundz[cz]], dtype='float64')
        new_RE = na.array([boundx[cx+1], boundy[cy+1], boundz[cz+1]], dtype='float64')

        new_vol = ((new_RE - new_LE) **2).sum()
        print 'P%04d weight %f old_vol %f new_vol %f ratio %f' % \
            (mi, weight, old_vol, new_vol, new_vol/old_vol)
        
        if padding > 0:
            return True, \
                new_LE, new_RE, self.hierarchy.periodic_region_strict(self.center, new_LE-padding, new_RE+padding)

        return False, new_LE, new_RE, self.hierarchy.region_strict(self.center, new_LE, new_RE)


    def _mpi_find_neighbor_3d(self, shift):
        """ Given a shift array, 1x3 long, find the task ID
        of that neighbor. For example, shift=[1,0,0] finds the neighbor
        immediately to the right in the positive x direction. Each task
        has 26 neighbors, of which some may be itself depending on the number
        and arrangement of tasks.
        """
        if not self._distributed: return 0
        shift = na.array(shift)
        cc = na.array(MPI.Compute_dims(MPI.COMM_WORLD.size, 3))
        mi = MPI.COMM_WORLD.rank
        si = MPI.COMM_WORLD.size
        # store some facts about myself
        mi_cx,mi_cy,mi_cz = na.unravel_index(mi,cc)
        mi_ar = na.array([mi_cx,mi_cy,mi_cz])
        # these are identical on all tasks
        # should these be calculated once and stored?
        #dLE = na.empty((si,3), dtype='float64') # positions not needed yet...
        #dRE = na.empty((si,3), dtype='float64')
        tasks = na.empty((cc[0],cc[1],cc[2]), dtype='int64')
        
        for i in range(si):
            cx,cy,cz = na.unravel_index(i,cc)
            tasks[cx,cy,cz] = i
            #x = na.mgrid[LE[0]:RE[0]:(cc[0]+1)*1j][cx:cx+2]
            #y = na.mgrid[LE[1]:RE[1]:(cc[1]+1)*1j][cy:cy+2]
            #z = na.mgrid[LE[2]:RE[2]:(cc[2]+1)*1j][cz:cz+2]
            #dLE[i, :] = na.array([x[0], y[0], z[0]], dtype='float64')
            #dRE[i, :] = na.array([x[1], y[1], z[1]], dtype='float64')
        
        # find the neighbor
        ne = (mi_ar + shift) % cc
        ne = tasks[ne[0],ne[1],ne[2]]
        return ne
        
        
    def _barrier(self):
        if not self._distributed: return
        mylog.debug("Opening MPI Barrier on %s", MPI.COMM_WORLD.rank)
        MPI.COMM_WORLD.Barrier()

    @parallel_passthrough
    def _mpi_catdict(self, data):
        self._barrier()
        field_keys = data.keys()
        field_keys.sort()
        np = MPI.COMM_WORLD.size
        for key in field_keys:
            mylog.debug("Joining %s (%s) on %s", key, type(data[key]),
                        MPI.COMM_WORLD.rank)
            if MPI.COMM_WORLD.rank == 0:
                temp_data = []
                if data[key] is not None: temp_data.append(data[key])
                for i in range(1,np):
                    buf = _recv_array(source=i, tag=0)
                    if buf is not None: temp_data.append(buf)
                data[key] = na.concatenate(temp_data, axis=-1)
            else:
                _send_array(data[key], dest=0, tag=0)
            self._barrier()
            data[key] = _bcast_array(data[key])
        self._barrier()
        return data

    @parallel_passthrough
    def _mpi_joindict(self, data):
        self._barrier()
        if MPI.COMM_WORLD.rank == 0:
            for i in range(1,MPI.COMM_WORLD.size):
                data.update(MPI.COMM_WORLD.recv(source=i, tag=0))
        else:
            MPI.COMM_WORLD.send(data, dest=0, tag=0)
        data = MPI.COMM_WORLD.bcast(data, root=0)
        self._barrier()
        return data

    @parallel_passthrough
    def _mpi_maxdict(self, data):
        """
        For each key in data, find the maximum value across all tasks, and
        then broadcast it back.
        """
        self._barrier()
        if MPI.COMM_WORLD.rank == 0:
            for i in range(1,MPI.COMM_WORLD.size):
                temp_data = MPI.COMM_WORLD.recv(source=i, tag=0)
                for key in temp_data:
                    try:
                        old_value = data[key]
                    except KeyError:
                        # This guarantees the new value gets added.
                        old_value = None
                    if old_value < temp_data[key]:
                        data[key] = temp_data[key]
        else:
            MPI.COMM_WORLD.send(data, dest=0, tag=0)
        data = MPI.COMM_WORLD.bcast(data, root=0)
        self._barrier()
        return data

    @parallel_passthrough
    def _mpi_maxdict_dict(self, data):
        """
        Similar to above, but finds maximums for dicts of dicts. This is
        specificaly for a part of chainHOP.
        """
        self._barrier()
        if MPI.COMM_WORLD.rank == 0:
            for i in range(1,MPI.COMM_WORLD.size):
                temp_data = MPI.COMM_WORLD.recv(source=i, tag=0)
                for top_key in temp_data:
                    # Make sure there's an entry for top_key in data
                    try:
                        test = data[top_key]
                    except KeyError:
                        data[top_key] = {}
                    for bot_key in temp_data[top_key]:
                        try:
                            old_value = data[top_key][bot_key]
                        except KeyError:
                            # This guarantees the new value gets added.
                            old_value = None
                        if old_value < temp_data[top_key][bot_key]:
                            data[top_key][bot_key] = temp_data[top_key][bot_key]
        else:
            MPI.COMM_WORLD.send(data, dest=0, tag=0)
        # Getting ghetto here, we're going to decompose the dict into arrays,
        # send that, and then reconstruct it. When data is too big the pickling
        # of the dict fails.
        size = 0
        if MPI.COMM_WORLD.rank == 0:
            top_keys = []
            bot_keys = []
            vals = []
            del temp_data
            count = 0
            for top_key in data:
                for bot_key in data[top_key]:
                    top_keys.append(top_key)
                    bot_keys.append(bot_key)
                    vals.append(data[top_key][bot_key])
            top_keys = na.array(top_keys, dtype='int64')
            bot_keys = na.array(bot_keys, dtype='int64')
            vals = na.array(vals, dtype='int64')
            size = top_keys.size
        # Broadcast them using array methods
        size = MPI.COMM_WORLD.bcast(size, root=0)
        if MPI.COMM_WORLD.rank != 0:
            top_keys = na.empty(size, dtype='int64')
            bot_keys = na.empty(size, dtype='int64')
            vals = na.empty(size, dtype='int64')
        MPI.COMM_WORLD.Bcast([top_keys,MPI.INT], root=0)
        MPI.COMM_WORLD.Bcast([bot_keys,MPI.INT], root=0)
        MPI.COMM_WORLD.Bcast([vals, MPI.FLOAT], root=0)
        # Convert it back into a dict where needed
        if MPI.COMM_WORLD.rank != 0:
            del data
            data = {}
            for i,top_key in enumerate(top_keys):
                try:
                    test = data[top_key]
                except KeyError:
                    data[top_key] = {}
                data[top_key][bot_keys[i]] = vals[i]
        del top_keys, bot_keys, vals
        self._barrier()
        return data

    @parallel_passthrough
    def __mpi_recvlist(self, data):
        # First we receive, then we make a new list.
        data = ensure_list(data)
        for i in range(1,MPI.COMM_WORLD.size):
            buf = ensure_list(MPI.COMM_WORLD.recv(source=i, tag=0))
            data += buf
        return data

    @parallel_passthrough
    def _mpi_catlist(self, data):
        self._barrier()
        if MPI.COMM_WORLD.rank == 0:
            data = self.__mpi_recvlist(data)
        else:
            MPI.COMM_WORLD.send(data, dest=0, tag=0)
        mylog.debug("Opening MPI Broadcast on %s", MPI.COMM_WORLD.rank)
        data = MPI.COMM_WORLD.bcast(data, root=0)
        self._barrier()
        return data

    @parallel_passthrough
    def __mpi_recvarrays(self, data):
        # First we receive, then we make a new list.
        for i in range(1,MPI.COMM_WORLD.size):
            buf = _recv_array(source=i, tag=0)
            if buf is not None: data = na.concatenate([data, buf])
        return data

    @parallel_passthrough
    def _mpi_catarray(self, data):
        self._barrier()
        if MPI.COMM_WORLD.rank == 0:
            data = self.__mpi_recvarrays(data)
        else:
            _send_array(data, dest=0, tag=0)
        mylog.debug("Opening MPI Broadcast on %s", MPI.COMM_WORLD.rank)
        data = _bcast_array(data, root=0)
        self._barrier()
        return data

    def _should_i_write(self):
        if not self._distributed: return True
        return (MPI.COMM_WORLD == 0)

    def _preload(self, grids, fields, queue):
        # This will preload if it detects we are parallel capable and
        # if so, we load *everything* that we need.  Use with some care.
        if not self._distributed: return
        queue.preload(grids, fields)

    @parallel_passthrough
    def _mpi_allsum(self, data):
        self._barrier()
        # We use old-school pickling here on the assumption the arrays are
        # relatively small ( < 1e7 elements )
        return MPI.COMM_WORLD.allreduce(data, op=MPI.SUM)

    @parallel_passthrough
    def _mpi_allmax(self, data):
        self._barrier()
        return MPI.COMM_WORLD.allreduce(data, op=MPI.MAX)

    @parallel_passthrough
    def _mpi_allmin(self, data):
        self._barrier()
        return MPI.COMM_WORLD.allreduce(data, op=MPI.MIN)

    def _mpi_size(self):
        if not self._distributed: return 0
        return MPI.COMM_WORLD.size

    def _mpi_info_dict(self, info):
        if not self._distributed: return 0, {0:info}
        self._barrier()
        data = None
        if MPI.COMM_WORLD.rank == 0:
            data = {0:info}
            for i in range(1, MPI.COMM_WORLD.size):
                data[i] = MPI.COMM_WORLD.recv(source=i, tag=0)
        else:
            MPI.COMM_WORLD.send(info, dest=0, tag=0)
        mylog.debug("Opening MPI Broadcast on %s", MPI.COMM_WORLD.rank)
        data = MPI.COMM_WORLD.bcast(data, root=0)
        self._barrier()
        return MPI.COMM_WORLD.rank, data

    def _get_dependencies(self, fields):
        deps = []
        fi = self.pf.field_info
        for field in fields:
            deps += ensure_list(fi[field].get_dependencies(pf=self.pf).requested)
        return list(set(deps))

    def _claim_object(self, obj):
        if not self._distributed: return
        obj._owner = MPI.COMM_WORLD.rank
        obj._distributed = True

    def _do_not_claim_object(self, obj):
        if not self._distributed: return
        obj._owner = -1
        obj._distributed = True

    def _write_on_root(self, fn):
        if not self._distributed: return open(fn, "w")
        if MPI.COMM_WORLD.rank == 0:
            return open(fn, "w")
        else:
            return cStringIO.StringIO()

    def _get_filename(self, prefix):
        if not self._distributed: return prefix
        return "%s_%03i" % (prefix, MPI.COMM_WORLD.rank)

    def _is_mine(self, obj):
        if not obj._distributed: return True
        return (obj._owner == MPI.COMM_WORLD.rank)

__tocast = 'c'

def _send_array(arr, dest, tag = 0):
    if not isinstance(arr, na.ndarray):
        MPI.COMM_WORLD.send((None,None), dest=dest, tag=tag)
        MPI.COMM_WORLD.send(arr, dest=dest, tag=tag)
        return
    tmp = arr.view(__tocast) # Cast to CHAR
    # communicate type and shape
    MPI.COMM_WORLD.send((arr.dtype.str, arr.shape), dest=dest, tag=tag)
    MPI.COMM_WORLD.Send([arr, MPI.CHAR], dest=dest, tag=tag)
    del tmp

def _recv_array(source, tag = 0):
    dt, ne = MPI.COMM_WORLD.recv(source=source, tag=tag)
    if dt is None and ne is None:
        return MPI.COMM_WORLD.recv(source=source, tag=tag)
    arr = na.empty(ne, dtype=dt)
    tmp = arr.view(__tocast)
    MPI.COMM_WORLD.Recv([tmp, MPI.CHAR], source=source, tag=tag)
    return arr

def _bcast_array(arr, root = 0):
    if MPI.COMM_WORLD.rank == root:
        tmp = arr.view(__tocast) # Cast to CHAR
        MPI.COMM_WORLD.bcast((arr.dtype.str, arr.shape), root=root)
    else:
        dt, ne = MPI.COMM_WORLD.bcast(None, root=root)
        arr = na.empty(ne, dtype=dt)
        tmp = arr.view(__tocast)
    MPI.COMM_WORLD.Bcast([tmp, MPI.CHAR], root=root)
    return arr
