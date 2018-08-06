#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and 
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain 
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

__all__ = [ 'SequentialDecomposition' ]

from pyomo.network import Port, Arc
from pyomo.core import Constraint, value, Objective, Var, ConcreteModel, \
    Binary, minimize, Expression
from pyomo.core.kernel.component_set import ComponentSet
from pyomo.core.kernel.component_map import ComponentMap
from pyomo.core.expr.current import identify_variables
from pyomo.repn import generate_standard_repn
from pyutilib.misc import Options
import copy, logging, time
from six import iteritems, itervalues

try:
    import networkx as nx
    import numpy
    imports_available = True
except ImportError:
    imports_available = False

logger = logging.getLogger('pyomo.network')


class SequentialDecomposition(object):
    """
    A sequential decomposition tool for Pyomo network models.

    Options, accessed via self.options:
        graph                   A networkx graph representing the model to
                                    be solved
                                    Default: None (will compute it)
        tear_set                A list of indexes representing edges to be
                                    torn. Can be set with a list of edge
                                    tuples via set_tear_set
                                    Default: None (will compute it)
        select_tear_method      Which method to use to select a tear set,
                                    either "mip" or "heuristic"
                                    Default: "mip"
        run_first_pass          Boolean indicating whether or not to run
                                    through network before running the
                                    tear stream convergence procedure
                                    Default: True
        solve_tears             Boolean indicating whether or not to run
                                    iterations to converge tear streams
                                    Default: True
        guesses                 ComponentMap of guesses to use for first pass
                                    (see set_guesses_for method)
                                    Default: ComponentMap()
        default_guess           Value to use if a free variable has no guess
                                    Default: None
        almost_equal_tol        Difference below which numbers are considered
                                    equal when checking port value agreement
                                    Default: 1.0E-8
        log_info                Set logger level to INFO during run
                                    Default: False
        tear_method             Method to use for converging tear streams,
                                    either "Direct" or "Wegstein"
                                    Default: "Direct"
        iterLim                 Limit on the number of tear iterations
                                    Default: 40
        tol                     Tolerance at which to stop tear iterations
                                    Default: 1.0E-5
        tol_type                Type of tolerance value, either:
                                    "abs" - Absolute
                                    "rel" - Relative (to current value)
                                    Default: "abs"
        report_diffs            Report the matrix of differences across tear
                                    streams for every iteration
                                    Default: False
        accel_min               Min value for Wegstein acceleration factor
                                    Default: -5
        accel_max               Max value for Wegstein acceleration factor
                                    Default: 0
        tear_solver             Name of solver to use for select_tear_mip
                                    Default: "cplex"
        tear_solver_io          Solver IO keyword for the above solver
                                    Default: "python"
        tear_solver_options     Keyword options to pass to solve method
                                    Default: {}
    """

    def __init__(self):
        if not imports_available:
            raise ImportError("This class requires numpy and networkx")

        self.cache = {}
        options = self.options = Options()
        # defaults
        options["graph"] = None
        options["tear_set"] = None
        options["select_tear_method"] = "mip"
        options["run_first_pass"] = True
        options["solve_tears"] = True
        options["guesses"] = ComponentMap()
        options["default_guess"] = None
        options["almost_equal_tol"] = 1.0E-8
        options["log_info"] = False
        options["tear_method"] = "Direct"
        options["iterLim"] = 40
        options["tol"] = 1.0E-5
        options["tol_type"] = "abs"
        options["report_diffs"] = False
        options["accel_min"] = -5
        options["accel_max"] = 0
        options["tear_solver"] = "cplex"
        options["tear_solver_io"] = "python"
        options["tear_solver_options"] = {}

    def set_guesses_for(self, port, guesses):
        """
        Set the guesses for the given port.

        These guesses will be checked for all free variables that are
        encountered during the first pass run. If a free variable has
        no guess, its current value will be used. If its current value
        is None, the default_guess option will be used. If that is None,
        an error will be raised.

        All port variables that are downstream of a non-tear edge will
        already be fixed. If there is a guess for a fixed variable, it
        will be silently ignored.

        The guesses should be a dict that maps the following:
            Port Member Name -> Value

        Or, for indexed members, multiple dicts that map:
            Port Member Name -> Index -> Value

        For extensive members, "Value" must be a list of tuples of the
        form (arc, value) to guess a value for the expanded variable
        of the specified arc. However, if the arc connecting this port
        is a 1-to-1 arc with its peer, then there will be no expanded
        variable for the single arc, so a regular "Value" should be
        provided.

        This dict cannot be used to pass guesses for variables within
        expression type members. Guesses for those variables must be
        assigned to the variable's current value before calling run.

        While this method makes things more convenient, all it does is:
            self.options["guesses"][port] = guesses
        """
        self.options["guesses"][port] = guesses

    def set_tear_set(self, tset):
        """
        Set a custom tear set to be used when running the decomposition.

        The procedure will use this custom tear set instead of finding
        its own, thus it can save some time. Additionally, this will be
        useful for knowing which edges will need guesses.

        Arguments:
            tset            A list of Arcs representing edges to tear

        While this method makes things more convenient, all it does is:
            self.options["tear_set"] = tset
        """
        self.options["tear_set"] = tset

    def tear_set_arcs(self, G, method="mip", **kwds):
        """
        Call the specified tear selection method and return a list
        of arcs representing the selected tear edges.

        The **kwds will be passed to the method.
        """
        if method == "mip":
            tset = self.select_tear_mip(G, **kwds)
        elif method == "heuristic":
            # tset is the first list in the first return value
            tset = self.select_tear_heuristic(G, **kwds)[0][0]
        else:
            raise ValueError("Invalid method '%s'" % (method,))

        return self.indexes_to_arcs(G, tset)

    def indexes_to_arcs(self, G, lst):
        """
        Converts a list of edge indexes to the corresponding Arcs.

        Arguments:
            G               A networkx graph corresponding to lst
            lst             A list of edge indexes to convert to tuples

        Returns:
            A list of arcs
        """
        edge_list = self.idx_to_edge(G)
        res = []
        for ei in lst:
            edge = edge_list[ei]
            res.append(G.edges[edge]["arc"])
        return res

    def run(self, model, function):
        """
        Compute a Pyomo network model using sequential decomposition.

        Arguments:
            model           A Pyomo model
            function        A function to be called on each block/node
                                in the network
        """
        if self.options["log_info"]:
            old_log_level = logger.level
            logger.setLevel(logging.INFO)

        start = time.time()
        logger.info("Starting Sequential Decomposition")

        self.cache.clear()

        G = self.options["graph"]
        if G is None:
            G = self.create_graph(model)

        tset = self.tear_set(G)

        if self.options["run_first_pass"]:
            logger.info("Starting first pass run of network")
            order = self.calculation_order(G)
            self.run_order(G, order, function, tset, use_guesses=True)

        if not self.options["solve_tears"] or not len(tset):
            # Not solving tears, we're done
            end = time.time()
            logger.info("Finished Sequential Decomposition in %.2f seconds" %
                (end - start))
            return

        logger.info("Starting tear convergence procedure")

        sccNodes, sccEdges, sccOrder, outEdges = self.scc_collect(G)

        for lev in sccOrder:
            for sccIndex in lev:
                order = self.calculation_order(G, nodes=sccNodes[sccIndex])

                # only pass tears that are part of this SCC
                tears = []
                for ei in tset:
                    if ei in sccEdges[sccIndex]:
                        tears.append(ei)

                kwds = dict(G=G, order=order, function=function, tears=tears,
                    iterLim=self.options["iterLim"], tol=self.options["tol"],
                    tol_type=self.options["tol_type"],
                    report_diffs=self.options["report_diffs"],
                    outEdges=outEdges[sccIndex])

                tear_method = self.options["tear_method"]

                if tear_method == "Direct":
                    self.solve_tear_direct(**kwds)

                elif tear_method == "Wegstein":
                    kwds["accel_min"] = self.options["accel_min"]
                    kwds["accel_max"] = self.options["accel_max"]
                    self.solve_tear_wegstein(**kwds)

                else:
                    raise ValueError(
                        "Invalid tear_method '%s'" % (tear_method,))

        self.cache.clear()

        end = time.time()
        logger.info("Finished Sequential Decomposition in %.2f seconds" %
            (end - start))

        if self.options["log_info"]:
            logger.setLevel(old_log_level)

    def run_order(self, G, order, function, ignore=None, use_guesses=False):
        """
        Run computations in the order provided by calling the function.

        Arguments:
            G               A networkx graph corresponding to order
            order           The order in which to run each node in the graph
            function        The function to be called on each block/node
            ignore          Edge indexes to ignore when passing values
            use_guesses     If True, will check the guesses dict when fixing
                                free variables before calling function
        """
        fixed_inputs = self.fixed_inputs()
        fixed_outputs = ComponentSet()
        edge_map = self.edge_to_idx(G)
        guesses = self.options["guesses"]
        default = self.options["default_guess"]
        for lev in order:
            for unit in lev:
                if unit not in fixed_inputs:
                    fixed_inputs[unit] = ComponentSet()
                fixed_ins = fixed_inputs[unit]

                # make sure all inputs are fixed
                for port in unit.component_data_objects(Port):
                    if not len(port.sources()):
                        continue
                    if use_guesses and port in guesses:
                        self.load_guesses(guesses, port, fixed_ins)
                    self.load_values(port, default, fixed_ins, use_guesses)

                function(unit)

                # free the inputs that were not already fixed
                for var in fixed_ins:
                    var.free()
                fixed_ins.clear()

                # pass the values downstream for all outlet ports
                for port in unit.component_data_objects(Port):
                    dests = port.dests()
                    if not len(dests):
                        continue
                    for var in port.iter_vars(expr_vars=True, fixed=False):
                        fixed_outputs.add(var)
                        var.fix()
                    for arc in dests:
                        arc_map = self.arc_to_edge(G)
                        if edge_map[arc_map[arc]] not in ignore:
                            self.pass_values(arc, fixed_inputs)
                    for var in fixed_outputs:
                        var.free()
                    fixed_outputs.clear()

    def pass_values(self, arc, fixed_inputs):
        """
        Pass the values from one unit to the next, recording only those that
        were not already fixed in the provided dict that maps blocks to sets.
        """
        eblock = arc.expanded_block
        src, dest = arc.src, arc.dest
        dest_unit = dest.parent_block()
        eq_tol = self.options["almost_equal_tol"]

        if dest_unit not in fixed_inputs:
            fixed_inputs[dest_unit] = ComponentSet()

        need_to_solve = False
        for con in eblock.component_data_objects(Constraint, active=True):
            # we expect to find equality constraints with one linear variable
            if not con.equality:
                # We assume every constraint here is an equality.
                # This will only be False if the transformation changes
                # or if the user puts something unexpected on the eblock.
                raise RuntimeError(
                    "Found inequality constraint '%s'. Please do not modify "
                    "the expanded block." % con.name)
            repn = generate_standard_repn(con.body)
            if repn.is_fixed():
                # the port member's peer was already fixed
                if abs(value(con.lower) - repn.constant) > eq_tol:
                    raise RuntimeError(
                        "Found connected ports '%s' and '%s' both with fixed "
                        "but different values (by > %s) for constraint '%s'" %
                        (src, dest, eq_tol, con.name))
                continue
            if not (repn.is_linear() and len(repn.linear_vars) == 1):
                raise RuntimeError(
                    "Constraint '%s' had more than one free variable when "
                    "trying to pass a value to its destination. Please fix "
                    "more variables before passing across this arc." % con.name)
            # fix the value of the single variable to satisfy the constraint
            # con.lower is usually a NumericConstant but call value on it
            # just in case it is something else
            val = (value(con.lower) - repn.constant) / repn.linear_coefs[0]
            var = repn.linear_vars[0]
            fixed_inputs[dest_unit].add(var)
            var.fix(val)

    def load_guesses(self, guesses, port, fixed):
        srcs = port.sources()
        for name, mem in iteritems(port.vars):
            try:
                entry = guesses[port][name]
            except KeyError:
                continue

            if isinstance(entry, dict):
                itr = [(mem[k], entry[k], k) for k in entry]
            elif mem.is_indexed():
                raise TypeError(
                    "Guess for indexed member '%s' in port '%s' must map to a "
                    "dict of indexes" % (name, port.name))
            else:
                itr = [(mem, entry, None)]

            for var, entry, idx in itr:
                if var.is_fixed():
                    # silently ignore vars already fixed
                    continue
                if (port.is_extensive(name) and
                        srcs[0].expanded_block.component(name) is not None):
                    for arc, val in entry:
                        evar = arc.expanded_block.component(name)[idx]
                        if evar.is_fixed():
                            # silently ignore vars already fixed
                            continue
                        fixed.add(evar)
                        evar.fix(val)
                elif var.is_expression_type():
                    raise ValueError(
                        "Cannot provide guess for expression type member "
                        "'%s%s' of port '%s', must set current value of "
                        "variables within expression" % (
                            name,
                            ("[%s]" % str(idx)) if mem.is_indexed() else "",
                            port.name))
                else:
                    fixed.add(var)
                    var.fix(entry)

    def load_values(self, port, default, fixed, use_guesses):
        sources = port.sources()
        for name, obj in port.iter_vars(fixed=False, names=True):
            evars = None
            if port.is_extensive(name):
                # collect evars if there are any
                if obj.is_indexed():
                    i = obj.index()
                    evars = [arc.expanded_block.component(name)[i]
                        for arc in sources]
                else:
                    evars = [arc.expanded_block.component(name)
                        for arc in sources]
                if evars[0] is None:
                    # no evars, so this arc is 1-to-1
                    evars = None
            if evars is not None:
                for evar in evars:
                    if evar.is_fixed():
                        continue
                    self.check_value_fix(port, evar, default, fixed,
                        use_guesses, extensive=True)
                # now all evars should be fixed so combine them
                # and fix the value of the extensive port member
                self.combine_and_fix(port, name, obj, evars, fixed)
            else:
                if obj.is_expression_type():
                    for var in identify_variables(obj,
                            include_fixed=False):
                        self.check_value_fix(port, var, default, fixed,
                            use_guesses)
                else:
                    self.check_value_fix(port, obj, default, fixed,
                        use_guesses)

    def check_value_fix(self, port, var, default, fixed, use_guesses,
            extensive=False):
        """
        Try to fix the var at its current value or the default, else error
        """
        val = None
        if var.value is not None:
            val = var.value
        elif default is not None:
            val = default

        if val is None:
            raise RuntimeError(
                "Encountered a free inlet %svariable '%s' %s port '%s' with no "
                "%scurrent value, or default_guess option, while attempting "
                "to compute the unit." % (
                    "extensive " if extensive else "",
                    var.name,
                    ("on", "to")[int(extensive)],
                    port.name,
                    "guess, " if use_guesses else ""))

        fixed.add(var)
        var.fix(val)

    def combine_and_fix(self, port, name, obj, evars, fixed):
        """
        For an extensive port member, combine the values of all
        expanded variables and fix the port member at their sum.
        Assumes that all expanded variables are fixed.
        """
        assert all(evar.is_fixed() for evar in evars)
        total = sum(value(evar) for evar in evars)
        self.pass_single_value(port, name, obj, total, fixed)

    def pass_single_value(self, port, name, member, val, fixed):
        """
        Fix the value of the port member and add it to the fixed set.
        If the member is an expression, appropriately fix the value of
        its free variable. Error if the member is already fixed but
        different from val, or if the member has more than one free
        variable."
        """
        eq_tol = self.options["almost_equal_tol"]
        if member.is_fixed():
            if abs(value(member) - val) > eq_tol:
                raise RuntimeError(
                    "Member '%s' of port '%s' is already fixed but has a "
                    "different value (by > %s) than what is being passed to it"
                    % (name, port.name, eq_tol))
        elif member.is_variable_type():
            fixed.add(member)
            member.fix(val)
        else:
            repn = generate_standard_repn(member - val)
            if repn.is_linear() and len(repn.linear_vars) == 1:
                # fix the value of the single variable
                fval = (0 - repn.constant) / repn.linear_coefs[0]
                var = repn.linear_vars[0]
                fixed.add(var)
                var.fix(fval)
            else:
                raise RuntimeError(
                    "Member '%s' of port '%s' had more than "
                    "one free variable when trying to pass a value "
                    "to it. Please fix more variables before passing "
                    "to this port." % (name, port.name))

    def source_dest_peer(self, arc, name, index=None):
        """
        Return the object that is the peer to the source port's member.
        This is either the destination port's member, or the variable
        on the arc's expanded block for Extensive properties. This will
        return the appropriate index of the peer.
        """
        # check the rule on source but dest should be the same
        if arc.src.is_extensive(name):
            evar = arc.expanded_block.component(name)
            if evar is not None:
                # 1-to-1 arcs don't make evar because they're an equality
                return evar[index]
        mem = arc.dest.vars[name]
        if mem.is_indexed():
            return mem[index]
        else:
            return mem

    def create_graph(self, model):
        """
        Returns a networkx MultiDiGraph of a Pyomo network model.

        The nodes are units and the edges follow Pyomo Arc objects. Nodes
        that get added to the graph are determined by the parent blocks
        of the source and destination Ports of every Arc in the model.
        Edges are added for each Arc using the direction specified by
        source and destination. All Arcs in the model will be used whether
        or not they are active (since this needs to be done after expansion),
        and they all need to be directed.
        """
        G = nx.MultiDiGraph()

        for arc in model.component_data_objects(Arc):
            if not arc.directed:
                raise ValueError("All Arcs must be directed when creating "
                                 "a graph for a model. Found undirected "
                                 "Arc: '%s'" % arc.name)
            if arc.expanded_block is None:
                raise ValueError("All Arcs must be expanded when creating "
                                 "a graph for a model. Found unexpanded "
                                 "Arc: '%s'" % arc.name)
            src, dest = arc.src.parent_block(), arc.dest.parent_block()
            G.add_edge(src, dest, arc=arc)

        return G

    def select_tear_mip_model(self, G):
        """
        Generate a model for selecting tears from the given graph.

        Returns:
            The model
            A list of the binary variables representing each edge,
                indexed by the edge index of the graph
        """
        model = ConcreteModel()

        bin_list = []
        for i in range(G.number_of_edges()):
            # add a binary "torn" variable for every edge
            vname = "edge%s" % i
            var = Var(domain=Binary)
            bin_list.append(var)
            model.add_component(vname, var)

        # var containing the maximum number of times any cycle is torn
        mct = model.max_cycle_tears = Var()

        _, cycleEdges = self.all_cycles(G)

        for i in range(len(cycleEdges)):
            ecyc = cycleEdges[i]

            # expression containing sum of tears for each cycle
            ename = "cycle_sum%s" % i
            expr = Expression(expr=sum(bin_list[i] for i in ecyc))
            model.add_component(ename, expr)

            # every cycle must have at least 1 tear
            cname_min = "cycle_min%s" % i
            con_min = Constraint(expr=expr >= 1)
            model.add_component(cname_min, con_min)

            # mct >= cycle_sum for all cycles, thus it becomes the max
            cname_mct = mct.name + "_geq%s" % i
            con_mct = Constraint(expr=mct >= expr)
            model.add_component(cname_mct, con_mct)

        # weigh the primary objective much greater than the secondary
        obj_expr = 1000 * mct + sum(var for var in bin_list)
        model.obj = Objective(expr=obj_expr, sense=minimize)

        return model, bin_list

    def select_tear_mip(self, G, solver, solver_io=None, solver_options={}):
        """
        This finds optimal sets of tear edges based on two criteria.
        The primary objective is to minimize the maximum number of
        times any cycle is broken. The seconday criteria is to
        minimize the number of tears.

        This function creates a MIP problem in Pyomo with a doubly
        weighted objective and solves it with the solver arguments.
        """
        model, bin_list = self.select_tear_mip_model(G)

        from pyomo.environ import SolverFactory
        opt = SolverFactory(solver, solver_io=solver_io)
        opt.solve(model, **solver_options)

        # collect final list by adding every edge with a "True" binary var
        tset = []
        for i in range(len(bin_list)):
            if bin_list[i].value == 1:
                tset.append(i)

        return tset

    def compute_err(self, svals, dvals, tol_type):
        if tol_type not in ("abs", "rel"):
            raise ValueError("Invalid tol_type '%s'" % (tol_type,))

        diff = svals - dvals
        if tol_type == "abs":
            err = diff
        else:
            # relative: divide by current value of svals
            old_settings = numpy.seterr(divide='ignore', invalid='ignore')
            err = diff / svals
            numpy.seterr(**old_settings)
            # isnan means 0/0 so diff is 0
            err[numpy.isnan(err)] = 0
            # isinf means diff/0, so just use the diff
            if any(numpy.isinf(err)):
                for i in range(len(err)):
                    if numpy.isinf(err[i]):
                        err[i] = diff[i]

        return err

    def tear_diff_direct(self, G, tears):
        """
        Returns numpy arrays of values for src and dest members
        for all edges in the tears list of edge indexes.
        """
        svals = []
        dvals = []
        edge_list = self.idx_to_edge(G)
        for tear in tears:
            arc = G.edges[edge_list[tear]]["arc"]
            src, dest = arc.src, arc.dest
            sf = arc.expanded_block.component("splitfrac")
            for name, mem in src.iter_vars(names=True):
                if sf is not None:
                    svals.append(value(mem * sf))
                else:
                    svals.append(value(mem))
                try:
                    index = mem.index()
                except AttributeError:
                    index = None
                dvals.append(value(self.source_dest_peer(arc, name, index)))
        svals = numpy.array(svals)
        dvals = numpy.array(dvals)
        return svals, dvals

    def pass_edges(self, G, edges):
        """Call pass values for a list of edge indexes"""
        fixed_outputs = ComponentSet()
        edge_list = self.idx_to_edge(G)
        for ei in edges:
            arc = G.edges[edge_list[ei]]["arc"]
            for var in arc.src.iter_vars(expr_vars=True, fixed=False):
                fixed_outputs.add(var)
                var.fix()
            self.pass_values(arc, self.fixed_inputs())
            for var in fixed_outputs:
                var.free()
            fixed_outputs.clear()


    def pass_tear_direct(self, G, tears):
        fixed_outputs = ComponentSet()
        edge_list = self.idx_to_edge(G)

        for tear in tears:
            # fix everything then call pass values
            arc = G.edges[edge_list[tear]]["arc"]
            for var in arc.src.iter_vars(expr_vars=True, fixed=False):
                fixed_outputs.add(var)
                var.fix()
            self.pass_values(arc, fixed_inputs=self.fixed_inputs())
            for var in fixed_outputs:
                var.free()
            fixed_outputs.clear()

    def pass_tear_wegstein(self, G, tears, x):
        """
        Set the destination value of all tear edges to
        the corresponding value in the numpy array x.
        """
        fixed_inputs = self.fixed_inputs()
        edge_list = self.idx_to_edge(G)
        i = 0
        for tear in tears:
            arc = G.edges[edge_list[tear]]["arc"]
            src, dest = arc.src, arc.dest
            dest_unit = dest.parent_block()

            if dest_unit not in fixed_inputs:
                fixed_inputs[dest_unit] = ComponentSet()

            for name, mem in src.iter_vars(names=True):
                try:
                    index = mem.index()
                except AttributeError:
                    index = None
                peer = self.source_dest_peer(arc, name, index)
                self.pass_single_value(dest, name, peer, x[i],
                    fixed_inputs[dest_unit])
                i += 1

    def generate_gofx(self, G, tears):
        edge_list = self.idx_to_edge(G)
        gofx = []
        for tear in tears:
            arc = G.edges[edge_list[tear]]["arc"]
            for mem in arc.src.iter_vars():
                sf = arc.expanded_block.component("splitfrac")
                if sf is not None:
                    gofx.append(value(mem * sf))
                else:
                    gofx.append(value(mem))
        gofx = numpy.array(gofx)
        return gofx

    def generate_first_x(self, G, tears):
        edge_list = self.idx_to_edge(G)
        x = []
        for tear in tears:
            arc = G.edges[edge_list[tear]]["arc"]
            for name, mem in arc.src.iter_vars(names=True):
                try:
                    index = mem.index()
                except AttributeError:
                    index = None
                peer = self.source_dest_peer(arc, name, index)
                x.append(value(peer))
        x = numpy.array(x)
        return x

    def cacher(self, key, fcn, *args):
        if key in self.cache:
            return self.cache[key]
        res = fcn(*args)
        self.cache[key] = res
        return res

    def tear_set(self, G):
        key = "tear_set"
        def fcn(G):
            tset = self.options[key]
            if tset is not None:
                arc_map = self.arc_to_edge(G)
                edge_map = self.edge_to_idx(G)
                res = []
                for arc in tset:
                    res.append(edge_map[arc_map[arc]])
                if not self.check_tear_set(G, res):
                    raise ValueError("Tear set found in options is "
                                     "insufficient to solve network")
                self.cache[key] = res
                return res

            method = self.options["select_tear_method"]
            if method == "mip":
                return self.select_tear_mip(G,
                                            self.options["tear_solver"],
                                            self.options["tear_solver_io"],
                                            self.options["tear_solver_options"])
            elif method == "heuristic":
                # tset is the first list in the first return value
                return self.select_tear_heuristic(G)[0][0]
            else:
                raise ValueError("Invalid select_tear_method '%s'" % (method,))
        return self.cacher(key, fcn, G)

    def arc_to_edge(self, G):
        """Returns a mapping from arcs to edges for a graph"""
        def fcn(G):
            res = ComponentMap()
            for edge in G.edges:
                arc = G.edges[edge]["arc"]
                res[arc] = edge
            return res
        return self.cacher("arc_to_edge", fcn, G)

    def fixed_inputs(self):
        return self.cacher("fixed_inputs", dict)

    def idx_to_node(self, G):
        """Returns a mapping from indexes to nodes for a graph"""
        return self.cacher("idx_to_node", list, G.nodes)

    def node_to_idx(self, G):
        """Returns a mapping from nodes to indexes for a graph"""
        def fcn(G):
            res = dict()
            i = -1
            for node in G.nodes:
                i += 1
                res[node] = i
            return res
        return self.cacher("node_to_idx", fcn, G)

    def idx_to_edge(self, G):
        """Returns a mapping from indexes to edges for a graph"""
        return self.cacher("idx_to_edge", list, G.edges)

    def edge_to_idx(self, G):
        """Returns a mapping from edges to indexes for a graph"""
        def fcn(G):
            res = dict()
            i = -1
            for edge in G.edges:
                i += 1
                res[edge] = i
            return res
        return self.cacher("edge_to_idx", fcn, G)

    ########################################################################
    #
    # The following code is adapted from graph.py in FOQUS:
    # https://github.com/CCSI-Toolset/FOQUS/blob/master/LICENSE.md
    # It has been modified to use networkx graphs and should be
    # independent of Pyomo or whatever the nodes actually are.
    #
    ########################################################################

    def solve_tear_direct(self, G, order, function, tears, outEdges, iterLim,
            tol, tol_type, report_diffs):
        """
        Use direct substitution to solve tears. If multiple tears are
        given they are solved simultaneously.

        Arguments:
            order           List of lists of order in which to calculate nodes
            tears           List of tear edge indexes
            iterLim         Limit on the number of iterations to run
            tol             Tolerance at which iteration can be stopped

        Returns:
            List of lists of diff history, differences between input and
                output values at each iteration.
        """
        hist = [] # diff at each iteration in every variable

        if not len(tears):
            # no need to iterate just run the calculations
            self.run_order(G, order, function, tears)
            return hist

        logger.info("Starting Direct tear convergence")

        ignore = tears + outEdges
        itercount = 0

        while True:
            svals, dvals = self.tear_diff_direct(G, tears)
            err = self.compute_err(svals, dvals, tol_type)
            hist.append(err)

            if report_diffs:
                print("Diff matrix:\n%s" % err)

            if numpy.max(numpy.abs(err)) < tol:
                break

            if itercount >= iterLim:
                logger.warning("Direct failed to converge in %s iterations"
                    % iterLim)
                return hist

            self.pass_tear_direct(G, tears)

            itercount += 1
            logger.info("Running Direct iteration %s" % itercount)
            self.run_order(G, order, function, ignore)

        self.pass_edges(G, outEdges)

        logger.info("Direct converged in %s iterations" % itercount)

        return hist

    def solve_tear_wegstein(self, G, order, function, tears, outEdges, iterLim,
        tol, tol_type, report_diffs, accel_min, accel_max):
        """
        Use Wegstein to solve tears. If multiple tears are given
        they are solved simultaneously.

        Arguments:
            order           List of lists of order in which to calculate nodes
            tears           List of tear edge indexes
            iterLim         Limit on the number of iterations to run
            tol             Tolerance at which iteration can be stopped
            accel_min        Minimum value for Wegstein acceleration factor
            accel_max        Maximum value for Wegstein acceleration factor
            tol_type     Interpretation of tolerance value, either:
                                "abs" - Absolute tolerance
                                "rel" - Relative tolerance (to bound range)

        Returns:
            List of lists of diff history, differences between input and
                output values at each iteration.
        """
        hist = [] # diff at each iteration in every variable

        if not len(tears):
            # no need to iterate just run the calculations
            self.run_order(G, order, function, tears)
            return hist

        logger.info("Starting Wegstein tear convergence")

        itercount = 0
        ignore = tears + outEdges

        gofx = self.generate_gofx(G, tears)
        x = self.generate_first_x(G, tears)

        err = self.compute_err(gofx, x, tol_type)
        hist.append(err)

        if report_diffs:
            print("Diff matrix:\n%s" % err)

        # check if it's already solved
        if numpy.max(numpy.abs(err)) < tol:
            logger.info("Wegstein converged in %s iterations" % itercount)
            return hist

        # if not solved yet do one direct step
        x_prev = x
        gofx_prev = gofx
        x = gofx
        self.pass_tear_wegstein(G, tears, gofx)

        while True:
            itercount += 1

            logger.info("Running Wegstein iteration %s" % itercount)
            self.run_order(G, order, function, ignore)

            gofx = self.generate_gofx(G, tears)

            err = self.compute_err(gofx, x, tol_type)
            hist.append(err)

            if report_diffs:
                print("Diff matrix:\n%s" % err)

            if numpy.max(numpy.abs(err)) < tol:
                break

            if itercount > iterLim:
                logger.warning("Wegstein failed to converge in %s iterations"
                    % iterLim)
                return hist

            denom = x - x_prev
            # this will divide by 0 at some points but we handle that below,
            # so ignore division warnings
            old_settings = numpy.seterr(divide='ignore', invalid='ignore')
            slope = numpy.divide((gofx - gofx_prev), denom)
            numpy.seterr(**old_settings)
            # if isnan or isinf then x and x_prev were the same,
            # so just do direct sub for those elements
            slope[numpy.isnan(slope)] = 0
            slope[numpy.isinf(slope)] = 0
            accel = slope / (slope - 1)
            accel[accel < accel_min] = accel_min
            accel[accel > accel_max] = accel_max
            x_prev = x
            gofx_prev = gofx
            x = accel * x_prev + (1 - accel) * gofx_prev
            self.pass_tear_wegstein(G, tears, x)

        self.pass_edges(G, outEdges)

        logger.info("Wegstein converged in %s iterations" % itercount)

        return hist

    def scc_collect(self, G, excludeEdges=None):
        """
        This is an algorithm for finding strongly connected components (SCCs)
        in a graph. It is based on Tarjan. 1972 Depth-First Search and Linear
        Graph Algorithms, SIAM J. Comput. v1 no. 2 1972

        Returns:
            List of lists of nodes in each SCC
            List of lists of edge indexes in each SCC
            List of lists for order in which to calculate SCCs
            List of lists of edge indexes leaving the SCC
        """
        def sc(v, stk, depth, strngComps):
            # recursive sub-function for backtracking
            ndepth[v] = depth
            back[v] = depth
            depth += 1
            stk.append(v)
            for w in adj[v]:
                if ndepth[w] == None:
                    sc(w, stk, depth, strngComps)
                    back[v] = min(back[w], back[v])
                elif w in stk:
                    back[v] = min(back[w], back[v])
            if back[v] == ndepth[v]:
                scomp = []
                while True:
                    w = stk.pop()
                    scomp.append(i2n[w])
                    if w == v:
                        break
                strngComps.append(scomp)
            return depth

        i2n, adj, _ = self.adj_lists(G, excludeEdges=excludeEdges)

        stk        = []  # node stack
        strngComps = []  # list of SCCs
        ndepth     = [None] * len(i2n)
        back       = [None] * len(i2n)

        # find the SCCs
        for v in range(len(i2n)):
            if ndepth[v] == None:
                sc(v, stk, 0, strngComps)

        # Find the rest of the information about SCCs given the node partition
        sccNodes = strngComps
        sccEdges = []
        outEdges = []
        inEdges = []
        for nset in strngComps:
            e, ie, oe = self.sub_graph_edges(G, nset)
            sccEdges.append(e)
            inEdges.append(ie)
            outEdges.append(oe)
        sccOrder = self.scc_calculation_order(sccNodes, inEdges, outEdges)
        return sccNodes, sccEdges, sccOrder, outEdges

    def scc_calculation_order(self, sccNodes, ie, oe):
        """
        This determines the order in which to do calculations for strongly
        connected components. It is used to help determine the most efficient
        order to solve tear streams. For example, if you have a graph like
        the following, you would want to do tear streams in SCC0 before SCC1
        and SCC2 to prevent extra iterations. This just makes an adjacency
        list with the SCCs as nodes and calls the tree order function.

        SCC0--+-->--SCC1
              |
              +-->--SCC2

        Arguments:
            sccNodes        List of lists of nodes in each SCC
            ie              List of lists of in edge indexes to SCCs
            oe              List of lists of out edge indexes to SCCs

        """
        adj = [] # SCC adjacency list
        adjR = [] # SCC reverse adjacency list
        # populate with empty lists before running the loop below
        for i in range(len(sccNodes)):
            adj.append([])
            adjR.append([])

        # build adjacency lists
        done = False
        for i in range(len(sccNodes)):
            for j in range(len(sccNodes)):
                for ine in ie[i]:
                    for oute in oe[j]:
                        if ine == oute:
                            adj[j].append(i)
                            adjR[i].append(j)
                            done = True
                    if done:
                        break
                if done:
                    break
            done = False

        return self.tree_order(adj, adjR)

    def calculation_order(self, G, roots=None, nodes=None):
        """
        Rely on tree_order to return a calculation order of nodes.

        Arguments:
            roots           List of nodes to consider as tree roots,
                                if None then the actual roots are used
            nodes           Subset of nodes to consider in the tree,
                                if None then all nodes are used
        """
        tset = self.tear_set(G)
        i2n, adj, adjR = self.adj_lists(G, excludeEdges=tset, nodes=nodes)

        order = []
        if roots is not None:
            node_map = self.node_to_idx(G)
            rootsIndex = []
            for node in roots:
                rootsIndex.append(node_map[node])
        else:
            rootsIndex = None

        orderIndex = self.tree_order(adj, adjR, rootsIndex)

        # convert indexes to actual nodes
        for i in range(len(orderIndex)):
            order.append([])
            for j in range(len(orderIndex[i])):
                order[i].append(i2n[orderIndex[i][j]])

        return order

    def tree_order(self, adj, adjR, roots=None):
        """
        This function determines the ordering of nodes in a directed
        tree. This is a generic function that can operate on any
        given tree represented by the adjaceny and reverse
        adjacency lists. If the adjacency list does not represent
        a tree the results are not valid.

        In the returned order, it is sometimes possible for more
        than one node to be caclulated at once. So a list of lists
        is returned by this function. These represent a bredth
        first search order of the tree. Following the order, all
        nodes that lead to a particular node will be visited
        before it.

        Arguments:
            adj: an adjeceny list for a directed tree. This uses
                generic integer node indexes, not node names from the
                graph itself. This allows this to be used on sub-graphs
                and graps of components more easily.
            adjR: the reverse adjacency list coresponing to adj
            roots: list of node indexes to start from. These do not
                need to be the root nodes of the tree, in some cases
                like when a node changes the changes may only affect
                nodes reachable in the tree from the changed node, in
                the case that roots are supplied not all the nodes in
                the tree may appear in the ordering. If no roots are
                supplied, the roots of the tree are used.
        """
        adjR = copy.deepcopy(adjR)
        for i, l in enumerate(adjR):
            adjR[i] = set(l)

        if roots is None:
            roots = []
            mark = [True] * len(adj) # mark all nodes if no roots specified
            r = [True] * len(adj)
            # no root specified so find roots of tree by marking every
            # successor of every node, since roots have no predecessors
            for sucs in adj:
                for i in sucs:
                    r[i] = False
            # make list of roots
            for i in range(len(r)):
                if r[i]:
                    roots.append(i)
        else:
            # if roots are specified mark descendants
            mark = [False] * len(adj)
            lst = roots
            while len(lst) > 0:
                lst2 = []
                for i in lst:
                    mark[i] = True
                    lst2 += adj[i]
                lst = set(lst2) # remove dupes

        # Now we have list of roots, and roots and their desendants are marked
        ndepth = [None] * len(adj)
        lst = copy.deepcopy(roots)
        order = []
        checknodes = set() # list of candidate nodes for next depth
        for i in roots: # nodes adjacent to roots are candidates
            checknodes.update(adj[i])
        depth = 0

        while len(lst) > 0:
            order.append(lst)
            depth += 1
            lst = [] # nodes to add to the next depth in order
            delSet = set() # nodes to delete from checknodes
            checkUpdate = set() # nodes to add to checknodes
            for i in checknodes:
                if ndepth[i] != None:
                    # This means there is a cycle in the graph
                    # this will lead to nonsense so throw exception
                    raise RuntimeError(
                        "Function tree_order does not work with cycles")
                remSet = set() # to remove from a nodes rev adj list
                for j in adjR[i]:
                    if j in order[depth - 1]:
                        # ancestor already placed
                        remSet.add(j)
                    elif mark[j] == False:
                        # ancestor doesn't descend from root
                        remSet.add(j)
                # delete parents from rev adj list if they were found
                # to be already placed or not in subgraph
                adjR[i] = adjR[i].difference(remSet)
                # if rev adj list is empty, all ancestors
                # have been placed so add node
                if len(adjR[i]) == 0:
                    ndepth[i] = depth
                    lst.append(i)
                    delSet.add(i)
                    checkUpdate.update(adj[i])
            # Delete the nodes that were added from the check set
            checknodes = checknodes.difference(delSet)
            checknodes = checknodes.union(checkUpdate)

        return order

    def check_tear_set(self, G, tset):
        """
        Check whether the specified tear streams are sufficient.
        If the graph minus the tear edges is not a tree then the
        tear set is not sufficient to solve the graph.
        """
        sccNodes, _, _, _ = self.scc_collect(G, excludeEdges=tset)
        for nodes in sccNodes:
            if len(nodes) > 1:
                return False
        return True

    def select_tear_heuristic(self, G):
        """
        This finds optimal sets of tear edges based on two criteria.
        The primary objective is to minimize the maximum number of
        times any cycle is broken. The seconday criteria is to
        minimize the number of tears.

        This function uses a branch and bound type approach.

        Returns:
            List of lists of tear sets. All the tear sets returned
                are equally good. There are often a very large number
                of equally good tear sets.
            The max number of times any single loop is torn
            The total number of loops

        Improvemnts for the future.
        I think I can imporve the efficency of this, but it is good
        enough for now. Here are some ideas for improvement:
            1) Reduce the number of redundant solutions. It is possible
               to find tears sets [1,2] and [2,1]. I eliminate
               redundent solutions from the results, but they can
               occur and it reduces efficency.
            2) Look at strongly connected components instead of whole
               graph. This would cut back on the size of graph we are
               looking at. The flowsheets are rarely one strongly
               conneted component.
            3) When you add an edge to a tear set you could reduce the
               size of the problem in the branch by only looking at
               strongly connected components with that edge removed.
            4) This returns all equally good optimal tear sets. That
               may not really be necessary. For very large flowsheets,
               there could be an extremely large number of optimial tear
               edge sets.
        """

        def sear(depth, prevY):
            # This is a recursive function for generating tear sets.
            # It selects one edge from a cycle, then calls itself
            # to select an edge from the next cycle.  It is a branch
            # and bound search tree to find best tear sets.

            # The function returns when all cycles are torn, which
            # may be before an edge was selected from each cycle if
            # cycles contain common edges.

            for i in range(len(cycleEdges[depth])):
                # Loop through all the edges in cycle with index depth
                y = list(prevY) # get list of already selected tear stream
                y[cycleEdges[depth][i]] = 1
                # calculate number of times each cycle is torn
                Ay = numpy.dot(A, y)
                maxAy = max(Ay)
                sumY = sum(y)
                if maxAy > upperBound[0]:
                    # breaking a cycle too many times, branch is no good
                    continue
                elif maxAy == upperBound[0] and sumY > upperBound[1]:
                    # too many tears, branch is no good
                    continue
                # Call self at next depth where a cycle is not broken
                if min(Ay) > 0:
                    if maxAy < upperBound[0]:
                        upperBound[0] = maxAy  # most important factor
                        upperBound[1] = sumY   # second most important
                    elif sumY < upperBound[1]:
                        upperBound[1] = sumY
                    # record solution
                    ySet.append([list(y), maxAy, sumY])
                else:
                    for j in range(depth + 1, nr):
                        if Ay[j] == 0:
                            sear(j, y)

        # Get a quick and I think pretty good tear set for upper bound
        tearUB = self.tear_upper_bound(G)

        # Find all the cycles in a graph and make cycle-edge matrix A
        # Rows of A are cycles and columns of A are edges
        # 1 if an edge is in a cycle, 0 otherwise
        A, _, cycleEdges = self.cycle_edge_matrix(G)
        (nr, nc) = A.shape

        if nr == 0:
            # no cycles so we are done
            return [[[]], 0 , 0]

        # Else there are cycles, so find edges to tear
        y_init = [False] * G.number_of_edges() # whether edge j is in tear set
        for j in tearUB:
            # y for initial u.b. solution
            y_init[j] = 1

        Ay_init = numpy.dot(A, y_init) # number of times each loop torn

        # Set two upper bounds. The fist upper bound is on number of times
        # a loop is broken. Second upper bound is on number of tears.
        upperBound = [max(Ay_init), sum(y_init)]

        y_init = [False] * G.number_of_edges() #clear y vector to start search
        ySet = []  # a list of tear sets
        # Three elements are stored in each tear set:
        # 0 = y vector (tear set), 1 = max(Ay), 2 = sum(y)

        # Call recursive function to find tear sets
        sear(0, y_init)

        # Screen tear sets found
        # A set can be recorded before upper bound is updated so we can
        # just throw out sets with objectives higher than u.b.
        deleteSet = []  # vector of tear set indexes to delete
        for i in range(len(ySet)):
            if ySet[i][1] > upperBound[0]:
                deleteSet.append(i)
            elif ySet[i][1] == upperBound[0] and ySet[i][2] > upperBound[1]:
                deleteSet.append(i)
        for i in reversed(deleteSet):
            del ySet[i]

        # Check for duplicates and delete them
        deleteSet = []
        for i in range(len(ySet) - 1):
            if i in deleteSet:
                continue
            for j in range(i + 1, len(ySet)):
                if j in deleteSet:
                    continue
                for k in range(len(y_init)):
                    eq = True
                    if ySet[i][0][k] != ySet[j][0][k]:
                        eq = False
                        break
                if eq == True:
                    deleteSet.append(j)
        for i in reversed(sorted(deleteSet)):
            del ySet[i]

        # Turn the binary y vectors into lists of edge indexes
        es = []
        for y in ySet:
            edges = []
            for i in range(len(y[0])):
                if y[0][i] == 1:
                    edges.append(i)
            es.append(edges)

        return es, upperBound[0], upperBound[1]

    def tear_upper_bound(self, G):
        """
        This function quickly finds a sub-optimal set of tear
        edges. This serves as an inital upperbound when looking
        for an optimal tear set. Having an inital upper bound
        improves efficiency.

        This works by constructing a search tree and just makes a
        tear set out of all the back edges.
        """

        def cyc(node, depth):
            # this is a recursive function
            depths[node] = depth
            depth += 1
            for edge in G.out_edges(node, keys=True):
                suc, key = edge[1], edge[2]
                if depths[suc] is None:
                    parents[suc] = node
                    cyc(suc, depth)
                elif depths[suc] < depths[node]:
                    # found a back edge, add to tear set
                    tearSet.append(edge_list.index((node, suc, key)))

        tearSet = []  # list of back/tear edges
        edge_list = self.idx_to_edge(G)
        depths = {}
        parents = {}

        for node in G.nodes:
            depths[node]  = None
            parents[node]  = None

        for node in G.nodes:
            if depths[node] is None:
                cyc(node, 0)

        return tearSet

    def sub_graph_edges(self, G, nodes):
        """
        This function returns a list of edge indexes that are
        included in a subgraph given by a list of nodes.

        Returns:
            List of edge indexes in the subgraph
            List of edge indexes starting outside the subgraph
                and ending inside
            List of edge indexes starting inside the subgraph
                and ending outside
        """
        e = []   # edges that connect two nodes in the subgraph
        ie = []  # in edges
        oe = []  # out edges
        edge_list = self.idx_to_edge(G)
        for i in range(G.number_of_edges()):
            src, dest, _ = edge_list[i]
            if src in nodes:
                if dest in nodes:
                    # it's in the sub graph
                    e.append(i)
                else:
                    # it's an out edge of the subgraph
                    oe.append(i)
            elif dest in nodes:
                #its a in edge of the subgraph
                ie.append(i)
        return e, ie, oe

    def cycle_edge_matrix(self, G):
        """
        Return a cycle-edge incidence matrix, a list of list of nodes in
        each cycle, and a list of list of edge indexes in each cycle.
        """
        cycleNodes, cycleEdges = self.all_cycles(G) # call cycle finding algorithm

        # Create empty incidence matrix and then fill it out
        ceMat = numpy.zeros((len(cycleEdges), G.number_of_edges()),
                            dtype=numpy.dtype(int))
        for i in range(len(cycleEdges)):
            for e in cycleEdges[i]:
                ceMat[i, e] = 1

        return ceMat, cycleNodes, cycleEdges

    def all_cycles(self, G):
        """
        This function finds all the cycles in a directed graph.
        The algorithm is based on Tarjan 1973 Enumeration of the
        elementary circuits of a directed graph, SIAM J. Comput. v3 n2 1973.

        Returns:
            List of lists of nodes in each cycle
            List of lists of edge indexes in each cycle
        """

        def backtrack(v, pre_key=None):
            # sub-function recursive part
            f = False
            pointStack.append((v, pre_key))
            mark[v] = True
            markStack.append(v)
            sucs = list(adj[v])

            for si, key in sucs:
                # iterate over successor indexes and keys
                if si < ni:
                    adj[v].remove((si, key))
                elif si == ni:
                    f = True
                    cyc = list(pointStack) # copy
                    # append the original point again so we get the last edge
                    cyc.append((si, key))
                    cycles.append(cyc)
                elif not mark[si]:
                    g = backtrack(si, key)
                    f = f or g

            if f:
                while markStack[-1] != v:
                    u = markStack.pop()
                    mark[u] = False
                markStack.pop()
                mark[v] = False

            pointStack.pop()
            return f

        i2n, adj, _ = self.adj_lists(G, multi=True)
        pointStack  = [] # stack of (node, key) tuples
        markStack = [] # nodes that have been marked
        cycles = [] # list of cycles found
        mark = [False] * len(i2n) # if a node is marked

        for ni in range(len(i2n)):
            # iterate over node indexes
            backtrack(ni)
            while len(markStack) > 0:
                i = markStack.pop()
                mark[i] = False

        # Turn node indexes back into nodes
        cycleNodes = []
        for cycle in cycles:
            cycleNodes.append([])
            for i in range(len(cycle)):
                ni, key = cycle[i]
                # change the node index in cycles to a node as well
                cycle[i] = (i2n[ni], key)
                cycleNodes[-1].append(i2n[ni])
            # pop the last node since it is the same as the first
            cycleNodes[-1].pop()

        # Now find list of edges in the cycle
        edge_map = self.edge_to_idx(G)
        cycleEdges = []
        for cyc in cycles:
            ecyc = []
            for i in range(len(cyc) - 1):
                pre, suc, key = cyc[i][0], cyc[i + 1][0], cyc[i + 1][1]
                ecyc.append(edge_map[(pre, suc, key)])
            cycleEdges.append(ecyc)

        return cycleNodes, cycleEdges

    def adj_lists(self, G, excludeEdges=None, nodes=None, multi=False):
        """
        Returns an adjacency list and a reverse adjacency list
        of node indexes for a MultiDiGraph.

        Arguments:
            G               A networkx MultiDiGraph
            excludeEdges    List of edge indexes to ignore when
                                considering neighbors
            nodes           List of nodes to form the adjacencies from
            multi           If True, adjacency lists will contains tuples
                                of (node, key) for every edge between
                                two nodes

        Returns:
            Map from index to node for all nodes included in nodes
            Adjacency list of successor indexes
            Reverse adjacency list of predecessor indexes
        """
        adj = []
        adjR = []

        exclude = set()
        if excludeEdges is not None:
            edge_list = self.idx_to_edge(G)
            for ei in excludeEdges:
                exclude.add(edge_list[ei])

        if nodes is None:
            nodes = self.idx_to_node(G)

        # we might not be including every node in these lists, so we need
        # custom maps to get between indexes and nodes
        i2n = [None] * len(nodes)
        n2i = dict()
        i = -1
        for node in nodes:
            i += 1
            n2i[node] = i
            i2n[i] = node

        i = -1
        for node in nodes:
            i += 1
            adj.append([])
            adjR.append([])

            seen = set()
            for edge in G.out_edges(node, keys=True):
                suc, key = edge[1], edge[2]
                if not multi and suc in seen:
                    # we only need to add the neighbor once
                    continue
                if suc in nodes and edge not in exclude:
                    # only add neighbor to seen if the edge is not excluded
                    seen.add(suc)
                    if multi:
                        adj[i].append((n2i[suc], key))
                    else:
                        adj[i].append(n2i[suc])

            seen = set()
            for edge in G.in_edges(node, keys=True):
                pre, key = edge[0], edge[2]
                if not multi and pre in seen:
                    continue
                if pre in nodes and edge not in exclude:
                    seen.add(pre)
                    if multi:
                        adjR[i].append((n2i[pre], key))
                    else:
                        adjR[i].append(n2i[pre])

        return i2n, adj, adjR
