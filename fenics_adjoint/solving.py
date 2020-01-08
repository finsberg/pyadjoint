import backend
import numpy
import ufl

from pyadjoint.block import Block
from pyadjoint.tape import get_working_tape, stop_annotating, annotate_tape
from pyadjoint.enlisting import Enlist
from .types import compat
from .types.function_space import extract_subfunction


# Type dependencies

# TODO: Clean up: some inaccurate comments. Reused code. Confusing naming with dFdm when denoting the control as c.


def solve(*args, **kwargs):
    """This solve routine wraps the real Dolfin solve call. Its purpose is to annotate the model,
    recording what solves occur and what forms are involved, so that the adjoint and tangent linear models may be
    constructed automatically by pyadjoint.

    To disable the annotation, just pass :py:data:`annotate=False` to this routine, and it acts exactly like the
    Dolfin solve call. This is useful in cases where the solve is known to be irrelevant or diagnostic
    for the purposes of the adjoint computation (such as projecting fields to other function spaces
    for the purposes of visualisation).

    The overloaded solve takes optional callback functions to extract adjoint solutions.
    All of the callback functions follow the same signature, taking a single argument of type Function.

    Keyword Args:
        adj_cb (function, optional): callback function supplying the adjoint solution in the interior.
            The boundary values are zero.
        adj_bdy_cb (function, optional): callback function supplying the adjoint solution on the boundary.
            The interior values are not guaranteed to be zero.
        adj2_cb (function, optional): callback function supplying the second-order adjoint solution in the interior.
            The boundary values are zero.
        adj2_bdy_cb (function, optional): callback function supplying the second-order adjoint solution on
            the boundary. The interior values are not guaranteed to be zero.

    """
    annotate = annotate_tape(kwargs)
    if annotate:
        tape = get_working_tape()

        solve_block_type = SolveVarFormBlock
        if not isinstance(args[0], ufl.equation.Equation):
            solve_block_type = SolveLinearSystemBlock

        sb_kwargs = solve_block_type.pop_kwargs(kwargs)
        sb_kwargs.update(kwargs)
        block = solve_block_type(*args, **sb_kwargs)
        tape.add_block(block)

    with stop_annotating():
        output = backend.solve(*args, **kwargs)

    if annotate:
        if hasattr(args[1], "create_block_variable"):
            block_variable = args[1].create_block_variable()
        else:
            block_variable = args[1].function.create_block_variable()
        block.add_output(block_variable)

    return output


class GenericSolveBlock(Block):
    pop_kwargs_keys = ["adj_cb", "adj_bdy_cb", "adj2_cb", "adj2_bdy_cb",
                       "forward_args", "forward_kwargs", "adj_args", "adj_kwargs"]

    def __init__(self, lhs, rhs, func, bcs, *args, **kwargs):
        super().__init__()
        self.adj_cb = kwargs.pop("adj_cb", None)
        self.adj_bdy_cb = kwargs.pop("adj_bdy_cb", None)
        self.adj2_cb = kwargs.pop("adj2_cb", None)
        self.adj2_bdy_cb = kwargs.pop("adj2_bdy_cb", None)
        self.adj_sol = None

        self.forward_args = []
        self.forward_kwargs = {}
        self.adj_args = []
        self.adj_kwargs = {}
        self.assemble_kwargs = {}

        # Equation LHS
        self.lhs = lhs
        # Equation RHS
        self.rhs = rhs
        # Solution function
        self.func = func
        self.function_space = self.func.function_space()
        # Boundary conditions
        self.bcs = []
        if bcs is not None:
            self.bcs = Enlist(bcs)

        if isinstance(self.lhs, ufl.Form) and isinstance(self.rhs, ufl.Form):
            self.linear = True
            for c in self.rhs.coefficients():
                self.add_dependency(c, no_duplicates=True)
        else:
            self.linear = False

        for c in self.lhs.coefficients():
            self.add_dependency(c, no_duplicates=True)

        for bc in self.bcs:
            self.add_dependency(bc, no_duplicates=True)

        if backend.__name__ != "firedrake":
            mesh = self.lhs.ufl_domain().ufl_cargo()
        else:
            mesh = self.lhs.ufl_domain()
        self.add_dependency(mesh)
        self._init_solver_parameters(args, kwargs)

    def _init_solver_parameters(self, args, kwargs):
        self.forward_args = kwargs.pop("forward_args", [])
        self.forward_kwargs = kwargs.pop("forward_kwargs", {})
        self.adj_args = kwargs.pop("adj_args", [])
        self.adj_kwargs = kwargs.pop("adj_kwargs", {})
        self.assemble_kwargs = {}

    def __str__(self):
        return "{} = {}".format(str(self.lhs), str(self.rhs))

    def _create_F_form(self):
        # Process the equation forms, replacing values with checkpoints,
        # and gathering lhs and rhs in one single form.
        if self.linear:
            tmp_u = compat.create_function(self.function_space)
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replace_map = self._replace_map(F_form)
        replace_map[tmp_u] = self.get_outputs()[0].saved_output
        return ufl.replace(F_form, replace_map)

    def _homogenize_bcs(self):
        bcs = []
        for bc in self.bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = compat.create_bc(bc, homogenize=True)
            bcs.append(bc)
        return bcs

    def _create_initial_guess(self):
        return backend.Function(self.function_space)

    def _recover_bcs(self):
        bcs = []
        for block_variable in self.get_dependencies():
            c = block_variable.output
            c_rep = block_variable.saved_output

            if isinstance(c, backend.DirichletBC):
                bcs.append(c_rep)
        return bcs

    def _replace_map(self, form):
        replace_coeffs = {}
        for block_variable in self.get_dependencies():
            coeff = block_variable.output
            if coeff in form.coefficients():
                replace_coeffs[coeff] = block_variable.saved_output
        return replace_coeffs

    def _replace_form(self, form, func=None):
        """Replace the form coefficients with checkpointed values

        func represents the initial guess if relevant.
        """
        replace_map = self._replace_map(form)
        if func is not None and self.func in replace_map:
            backend.Function.assign(func, replace_map[self.func])
            replace_map[self.func] = func
        return ufl.replace(form, replace_map)

    @staticmethod
    def _should_compute_boundary_adjoint(relevant_dependencies):
        # Check if DirichletBC derivative is relevant
        bdy = False
        for _, dep in relevant_dependencies:
            if isinstance(dep.output, backend.DirichletBC):
                bdy = True
                break
        return bdy

    def prepare_evaluate_adj(self, inputs, adj_inputs, relevant_dependencies):
        fwd_block_variable = self.get_outputs()[0]
        u = fwd_block_variable.output

        dJdu = adj_inputs[0]

        F_form = self._create_F_form()

        dFdu = backend.derivative(F_form, fwd_block_variable.saved_output, backend.TrialFunction(u.function_space()))
        dFdu_form = backend.adjoint(dFdu)
        dJdu = dJdu.copy()

        compute_bdy = self._should_compute_boundary_adjoint(relevant_dependencies)
        adj_sol, adj_sol_bdy = self._assemble_and_solve_adj_eq(dFdu_form, dJdu, compute_bdy)
        self.adj_sol = adj_sol
        if self.adj_cb is not None:
            self.adj_cb(adj_sol)
        if self.adj_bdy_cb is not None and compute_bdy:
            self.adj_bdy_cb(adj_sol_bdy)

        r = {}
        r["form"] = F_form
        r["adj_sol"] = adj_sol
        r["adj_sol_bdy"] = adj_sol_bdy
        return r

    def _assemble_and_solve_adj_eq(self, dFdu_adj_form, dJdu, compute_bdy):
        dJdu_copy = dJdu.copy()
        kwargs = self.assemble_kwargs.copy()
        # Homogenize and apply boundary conditions on adj_dFdu and dJdu.
        bcs = self._homogenize_bcs()
        kwargs["bcs"] = bcs
        dFdu = compat.assemble_adjoint_value(dFdu_adj_form, **kwargs)

        for bc in bcs:
            bc.apply(dJdu)

        adj_sol = compat.create_function(self.function_space)
        compat.linalg_solve(dFdu, adj_sol.vector(), dJdu, *self.adj_args, **self.adj_kwargs)

        adj_sol_bdy = None
        if compute_bdy:
            adj_sol_bdy = compat.function_from_vector(self.function_space, dJdu_copy - compat.assemble_adjoint_value(
                backend.action(dFdu_adj_form, adj_sol)))

        return adj_sol, adj_sol_bdy

    def evaluate_adj_component(self, inputs, adj_inputs, block_variable, idx, prepared=None):
        if not self.linear and self.func == block_variable.output:
            # We are not able to calculate derivatives wrt initial guess.
            return None
        F_form = prepared["form"]
        adj_sol = prepared["adj_sol"]
        adj_sol_bdy = prepared["adj_sol_bdy"]
        c = block_variable.output
        c_rep = block_variable.saved_output

        if isinstance(c, backend.Function):
            trial_function = backend.TrialFunction(c.function_space())
        elif isinstance(c, backend.Constant):
            mesh = compat.extract_mesh_from_form(F_form)
            trial_function = backend.TrialFunction(c._ad_function_space(mesh))
        elif isinstance(c, compat.ExpressionType):
            mesh = F_form.ufl_domain().ufl_cargo()
            c_fs = c._ad_function_space(mesh)
            trial_function = backend.TrialFunction(c_fs)
        elif isinstance(c, backend.DirichletBC):
            tmp_bc = compat.create_bc(c, value=extract_subfunction(adj_sol_bdy, c.function_space()))
            return [tmp_bc]
        elif isinstance(c, compat.MeshType):
            # Using CoordianteDerivative requires us to do action before
            # differentiating, might change in the future.
            F_form_tmp = backend.action(F_form, adj_sol)
            X = backend.SpatialCoordinate(c_rep)
            dFdm = backend.derivative(-F_form_tmp, X, backend.TestFunction(c._ad_function_space()))

            dFdm = compat.assemble_adjoint_value(dFdm, **self.assemble_kwargs)
            return dFdm

        dFdm = -backend.derivative(F_form, c_rep, trial_function)
        dFdm = backend.adjoint(dFdm)
        dFdm = dFdm * adj_sol
        dFdm = compat.assemble_adjoint_value(dFdm, **self.assemble_kwargs)
        if isinstance(c, compat.ExpressionType):
            return [[dFdm, c_fs]]
        else:
            return dFdm

    def prepare_evaluate_tlm(self, inputs, tlm_inputs, relevant_outputs):
        fwd_block_variable = self.get_outputs()[0]
        u = fwd_block_variable.output

        F_form = self._create_F_form()

        # Obtain dFdu.
        dFdu = backend.derivative(F_form, fwd_block_variable.saved_output, backend.TrialFunction(u.function_space()))

        return {
            "form": F_form,
            "dFdu": dFdu
        }

    def evaluate_tlm_component(self, inputs, tlm_inputs, block_variable, idx, prepared=None):
        F_form = prepared["form"]
        dFdu = prepared["dFdu"]
        V = self.get_outputs()[idx].output.function_space()

        bcs = []
        dFdm = 0.
        for block_variable in self.get_dependencies():
            tlm_value = block_variable.tlm_value
            c = block_variable.output
            c_rep = block_variable.saved_output

            if isinstance(c, backend.DirichletBC):
                if tlm_value is None:
                    bcs.append(compat.create_bc(c, homogenize=True))
                else:
                    bcs.append(tlm_value)
                continue
            elif isinstance(c, compat.MeshType):
                X = backend.SpatialCoordinate(c)
                c_rep = X

            if tlm_value is None:
                continue

            if c == self.func and not self.linear:
                continue

            dFdm += backend.derivative(-F_form, c_rep, tlm_value)

        if isinstance(dFdm, float):
            v = dFdu.arguments()[0]
            dFdm = backend.inner(backend.Constant(numpy.zeros(v.ufl_shape)), v) * backend.dx
        dFdm = ufl.algorithms.expand_derivatives(dFdm)
        dFdm = compat.assemble_adjoint_value(dFdm)
        dudm = backend.Function(V)
        return self._assemble_and_solve_tlm_eq(compat.assemble_adjoint_value(dFdu, bcs=bcs), dFdm, dudm, bcs)

    def _assemble_and_solve_tlm_eq(self, dFdu, dFdm, dudm, bcs):
        return self._assembled_solve(dFdu, dFdm, dudm, bcs)

    def _assemble_soa_eq_rhs(self, dFdu_form, adj_sol, hessian_input, d2Fdu2):
        # Start piecing together the rhs of the soa equation
        b = hessian_input.copy()
        if len(d2Fdu2.integrals()) > 0:
            b_form = backend.action(backend.adjoint(d2Fdu2), adj_sol)
        else:
            b_form = d2Fdu2

        for bo in self.get_dependencies():
            c = bo.output
            c_rep = bo.saved_output
            tlm_input = bo.tlm_value

            if (c == self.func and not self.linear) or tlm_input is None:
                continue

            if isinstance(c, compat.MeshType):
                X = backend.SpatialCoordinate(c)
                dFdu_adj = backend.action(backend.adjoint(dFdu_form), adj_sol)
                d2Fdudm = ufl.algorithms.expand_derivatives(
                    backend.derivative(dFdu_adj, X, tlm_input))
                if len(d2Fdudm.integrals()) > 0:
                    b_form += d2Fdudm
            elif not isinstance(c, backend.DirichletBC):
                dFdu_adj = backend.action(backend.adjoint(dFdu_form), adj_sol)
                b_form += backend.derivative(dFdu_adj, c_rep, tlm_input)

        b_form = ufl.algorithms.expand_derivatives(b_form)
        if len(b_form.integrals()) > 0:
            b -= compat.assemble_adjoint_value(b_form)

        return b

    def _assemble_and_solve_soa_eq(self, dFdu_form, adj_sol, hessian_input, d2Fdu2, compute_bdy):
        b = self._assemble_soa_eq_rhs(dFdu_form, adj_sol, hessian_input, d2Fdu2)
        dFdu_form = backend.adjoint(dFdu_form)
        adj_sol2, adj_sol2_bdy = self._assemble_and_solve_adj_eq(dFdu_form, b, compute_bdy)
        if self.adj2_cb is not None:
            self.adj2_cb(adj_sol2)
        if self.adj2_bdy_cb is not None and compute_bdy:
            self.adj2_bdy_cb(adj_sol2_bdy)
        return adj_sol2, adj_sol2_bdy

    def prepare_evaluate_hessian(self, inputs, hessian_inputs, adj_inputs, relevant_dependencies):
        # First fetch all relevant values
        fwd_block_variable = self.get_outputs()[0]
        hessian_input = hessian_inputs[0]
        tlm_output = fwd_block_variable.tlm_value

        if hessian_input is None:
            return

        if tlm_output is None:
            return

        F_form = self._create_F_form()

        # Using the equation Form we derive dF/du, d^2F/du^2 * du/dm * direction.
        dFdu_form = backend.derivative(F_form, fwd_block_variable.saved_output)
        d2Fdu2 = ufl.algorithms.expand_derivatives(
            backend.derivative(dFdu_form, fwd_block_variable.saved_output, tlm_output))

        adj_sol = self.adj_sol
        if adj_sol is None:
            raise RuntimeError("Hessian computation was run before adjoint.")
        bdy = self._should_compute_boundary_adjoint(relevant_dependencies)
        adj_sol2, adj_sol2_bdy = self._assemble_and_solve_soa_eq(dFdu_form, adj_sol, hessian_input, d2Fdu2, bdy)

        r = {}
        r["adj_sol2"] = adj_sol2
        r["adj_sol2_bdy"] = adj_sol2_bdy
        r["form"] = F_form
        r["adj_sol"] = adj_sol
        return r

    def evaluate_hessian_component(self, inputs, hessian_inputs, adj_inputs, block_variable, idx,
                                   relevant_dependencies, prepared=None):
        c = block_variable.output
        if c == self.func and not self.linear:
            return None

        adj_sol2 = prepared["adj_sol2"]
        adj_sol2_bdy = prepared["adj_sol2_bdy"]
        F_form = prepared["form"]
        adj_sol = prepared["adj_sol"]
        fwd_block_variable = self.get_outputs()[0]
        tlm_output = fwd_block_variable.tlm_value

        c_rep = block_variable.saved_output

        # If m = DirichletBC then d^2F(u,m)/dm^2 = 0 and d^2F(u,m)/dudm = 0,
        # so we only have the term dF(u,m)/dm * adj_sol2
        if isinstance(c, backend.DirichletBC):
            tmp_bc = compat.create_bc(c, value=extract_subfunction(adj_sol2_bdy, c.function_space()))
            return [tmp_bc]

        if isinstance(c_rep, backend.Constant):
            mesh = compat.extract_mesh_from_form(F_form)
            W = c._ad_function_space(mesh)
        elif isinstance(c, compat.ExpressionType):
            mesh = F_form.ufl_domain().ufl_cargo()
            W = c._ad_function_space(mesh)
        elif isinstance(c, compat.MeshType):
            X = backend.SpatialCoordinate(c)
            W = c._ad_function_space()
        else:
            W = c.function_space()

        dc = backend.TestFunction(W)
        form_adj = backend.action(F_form, adj_sol)
        form_adj2 = backend.action(F_form, adj_sol2)
        if isinstance(c, compat.MeshType):
            dFdm_adj = backend.derivative(form_adj, X, dc)
            dFdm_adj2 = backend.derivative(form_adj2, X, dc)
        else:
            dFdm_adj = backend.derivative(form_adj, c_rep, dc)
            dFdm_adj2 = backend.derivative(form_adj2, c_rep, dc)

        # TODO: Old comment claims this might break on split. Confirm if true or not.
        d2Fdudm = ufl.algorithms.expand_derivatives(
            backend.derivative(dFdm_adj, fwd_block_variable.saved_output,
                               tlm_output))

        d2Fdm2 = 0
        # We need to add terms from every other dependency
        # i.e. the terms d^2F/dm_1dm_2
        for _, bv in relevant_dependencies:
            c2 = bv.output
            c2_rep = bv.saved_output

            if isinstance(c2, backend.DirichletBC):
                continue

            tlm_input = bv.tlm_value
            if tlm_input is None:
                continue

            if c2 == self.func and not self.linear:
                continue

            # TODO: If tlm_input is a Sum, this crashes in some instances?
            if isinstance(c2_rep, compat.MeshType):
                X = backend.SpatialCoordinate(c2_rep)
                d2Fdm2 += ufl.algorithms.expand_derivatives(backend.derivative(dFdm_adj, X, tlm_input))
            else:
                d2Fdm2 += ufl.algorithms.expand_derivatives(backend.derivative(dFdm_adj, c2_rep, tlm_input))

        hessian_form = ufl.algorithms.expand_derivatives(d2Fdm2 + dFdm_adj2 + d2Fdudm)
        hessian_output = 0
        if not hessian_form.empty():
            hessian_output -= compat.assemble_adjoint_value(hessian_form)

        if isinstance(c, compat.ExpressionType):
            return [(hessian_output, W)]
        else:
            return hessian_output

    def prepare_recompute_component(self, inputs, relevant_outputs):
        return self._replace_recompute_form()

    def _replace_recompute_form(self):
        func = self._create_initial_guess()

        bcs = self._recover_bcs()
        lhs = self._replace_form(self.lhs, func=func)
        rhs = 0
        if self.linear:
            rhs = self._replace_form(self.rhs)

        return lhs, rhs, func, bcs

    def _forward_solve(self, lhs, rhs, func, bcs):
        backend.solve(lhs == rhs, func, bcs, *self.forward_args, **self.forward_kwargs)
        return func

    def _assembled_solve(self, lhs, rhs, func, bcs, **kwargs):
        for bc in bcs:
            bc.apply(rhs)
        backend.solve(lhs, func.vector(), rhs, **kwargs)
        return func

    def recompute_component(self, inputs, block_variable, idx, prepared):
        lhs = prepared[0]
        rhs = prepared[1]
        func = prepared[2]
        bcs = prepared[3]
        return self._forward_solve(lhs, rhs, func, bcs)


class SolveLinearSystemBlock(GenericSolveBlock):
    def __init__(self, A, u, b, *args, **kwargs):
        lhs = A.form
        func = u.function
        rhs = b.form
        bcs = A.bcs if hasattr(A, "bcs") else []
        super().__init__(lhs, rhs, func, bcs, *args, **kwargs)

        # Set up parameters initialization
        self.ident_zeros_tol = A.ident_zeros_tol if hasattr(A, "ident_zeros_tol") else None
        self.assemble_system = A.assemble_system if hasattr(A, "assemble_system") else False

    def _init_solver_parameters(self, args, kwargs):
        super()._init_solver_parameters(args, kwargs)
        if len(self.forward_args) <= 0:
            self.forward_args = args

        if len(self.adj_args) <= 0:
            self.adj_args = self.forward_args

    def _assemble_and_solve_adj_eq(self, dFdu_adj_form, dJdu, compute_bdy):
        dJdu_copy = dJdu.copy()
        bcs = self._homogenize_bcs()
        if self.assemble_system:
            rhs_bcs_form = backend.inner(backend.Function(self.function_space),
                                         dFdu_adj_form.arguments()[0]) * backend.dx
            A, _ = backend.assemble_system(dFdu_adj_form, rhs_bcs_form, bcs)
        else:
            A = backend.assemble(dFdu_adj_form)
            [bc.apply(A) for bc in bcs]

        [bc.apply(dJdu) for bc in bcs]

        adj_sol = compat.create_function(self.function_space)
        compat.linalg_solve(A, adj_sol.vector(), dJdu, *self.adj_args, **self.adj_kwargs)

        adj_sol_bdy = None
        if compute_bdy:
            adj_sol_bdy = compat.function_from_vector(self.function_space, dJdu_copy - compat.assemble_adjoint_value(
                backend.action(dFdu_adj_form, adj_sol)))

        return adj_sol, adj_sol_bdy

    def _forward_solve(self, lhs, rhs, func, bcs, **kwargs):
        if self.assemble_system:
            A, b = backend.assemble_system(lhs, rhs, bcs)
        else:
            A = backend.assemble(lhs)
            b = backend.assemble(rhs)
            [bc.apply(A, b) for bc in bcs]

        if self.ident_zeros_tol is not None:
            A.ident_zeros(self.ident_zeros_tol)

        backend.solve(A, func.vector(), b, *self.forward_args, **self.forward_kwargs)
        return func


class SolveVarFormBlock(GenericSolveBlock):
    pop_kwargs_keys = GenericSolveBlock.pop_kwargs_keys

    def __init__(self, equation, func, bcs=[], *args, **kwargs):
        lhs = equation.lhs
        rhs = equation.rhs
        super().__init__(lhs, rhs, func, bcs, *args, **kwargs)

    def _init_solver_parameters(self, args, kwargs):
        super()._init_solver_parameters(args, kwargs)
        if len(self.forward_args) <= 0:
            self.forward_args = args

        if len(self.forward_kwargs) <= 0:
            self.forward_kwargs = kwargs

        if "solver_parameters" in self.forward_kwargs and "mat_type" in self.forward_kwargs["solver_parameters"]:
            self.assemble_kwargs["mat_type"] = self.forward_kwargs["solver_parameters"]["mat_type"]

        if len(self.adj_kwargs) <= 0:
            solver_parameters = kwargs.get("solver_parameters", {})
            if "linear_solver" in solver_parameters:
                adj_args = [solver_parameters["linear_solver"]]
                if "preconditioner" in solver_parameters:
                    adj_args.append(solver_parameters["preconditioner"])
                self.adj_args = tuple(adj_args)
            self.adj_kwargs = solver_parameters

    def _assemble_and_solve_adj_eq(self, dFdu_adj_form, dJdu, compute_bdy=True):
        dJdu_copy = dJdu.copy()
        dFdu = compat.assemble_adjoint_value(dFdu_adj_form, **self.assemble_kwargs)
        bcs = self._homogenize_bcs()

        # Apply boundary conditions on adj_dFdu and dJdu.
        for bc in bcs:
            bc.apply(dFdu, dJdu)

        adj_sol = compat.create_function(self.function_space)
        lu_solver_methods = backend.lu_solver_methods()
        solver_method = self.adj_args[0] if len(self.adj_args) >= 1 else "default"
        solver_method = "default" if solver_method == "lu" else solver_method

        if solver_method in lu_solver_methods:
            solver = backend.LUSolver(solver_method)
            solver_parameters = self.adj_kwargs.get("lu_solver", {})
        else:
            solver = backend.KrylovSolver(*self.adj_args)
            solver_parameters = self.adj_kwargs.get("krylov_solver", {})
        solver.parameters.update(solver_parameters)
        solver.solve(dFdu, adj_sol.vector(), dJdu)

        adj_sol_bdy = None
        if compute_bdy:
            adj_sol_bdy = compat.function_from_vector(self.function_space, dJdu_copy - compat.assemble_adjoint_value(
                backend.action(dFdu_adj_form, adj_sol)))

        return adj_sol, adj_sol_bdy
