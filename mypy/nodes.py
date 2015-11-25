"""Abstract syntax tree node classes (i.e. parse tree)."""

import os
import re
from abc import abstractmethod, ABCMeta

from typing import (
    Any, overload, TypeVar, List, Tuple, cast, Set, Dict, Union, Optional
)

from mypy.lex import Token
import mypy.strconv
from mypy.visitor import NodeVisitor
from mypy.util import dump_tagged, short_type


class Context(metaclass=ABCMeta):
    """Base type for objects that are valid as error message locations."""
    @abstractmethod
    def get_line(self) -> int: pass


if False:
    # break import cycle only needed for mypy
    import mypy.types


T = TypeVar('T')


# Symbol table node kinds
#
# TODO rename to use more descriptive names

LDEF = 0  # type: int
GDEF = 1  # type: int
MDEF = 2  # type: int
MODULE_REF = 3  # type: int
# Type variable declared using TypeVar(...) has kind UNBOUND_TVAR. It's not
# valid as a type. A type variable is valid as a type (kind TVAR) within
# (1) a generic class that uses the type variable as a type argument or
# (2) a generic function that refers to the type variable in its signature.
UNBOUND_TVAR = 4  # type: int
BOUND_TVAR = 5  # type: int
TYPE_ALIAS = 6  # type: int


LITERAL_YES = 2
LITERAL_TYPE = 1
LITERAL_NO = 0

node_kinds = {
    LDEF: 'Ldef',
    GDEF: 'Gdef',
    MDEF: 'Mdef',
    MODULE_REF: 'ModuleRef',
    UNBOUND_TVAR: 'UnboundTvar',
    BOUND_TVAR: 'Tvar',
}


implicit_module_attrs = {'__name__': '__builtins__.str',
                         '__doc__': '__builtins__.str',
                         '__file__': '__builtins__.str'}


type_aliases = {
    'typing.List': '__builtins__.list',
    'typing.Dict': '__builtins__.dict',
    'typing.Set': '__builtins__.set',
}

reverse_type_aliases = dict((name.replace('__builtins__', 'builtins'), alias)
                            for alias, name in type_aliases.items())  # type: Dict[str, str]


class Node(Context):
    """Common base class for all non-type parse tree nodes."""

    line = -1

    literal = LITERAL_NO
    literal_hash = None  # type: Any

    def __str__(self) -> str:
        ans = self.accept(mypy.strconv.StrConv())
        if ans is None:
            return repr(self)
        return ans

    def set_line(self, target: Union[Token, int]) -> 'Node':
        if isinstance(target, int):
            self.line = target
        else:
            self.line = target.line
        return self

    def get_line(self) -> int:
        # TODO this should be just 'line'
        return self.line

    def accept(self, visitor: NodeVisitor[T]) -> T:
        raise RuntimeError('Not implemented')


class SymbolNode(Node):
    # Nodes that can be stored in a symbol table.

    # TODO do not use methods for these

    @abstractmethod
    def name(self) -> str: pass

    @abstractmethod
    def fullname(self) -> str: pass


class MypyFile(SymbolNode):
    """The abstract syntax tree of a single source file."""

    # Module name ('__main__' for initial file)
    _name = None      # type: str
    # Fully qualified module name
    _fullname = None  # type: str
    # Path to the file (None if not known)
    path = ''
    # Top-level definitions and statements
    defs = None  # type: List[Node]
    # Is there a UTF-8 BOM at the start?
    is_bom = False
    names = None  # type: SymbolTable
    # All import nodes within the file (also ones within functions etc.)
    imports = None  # type: List[ImportBase]
    # Lines to ignore when checking
    ignored_lines = None  # type: Set[int]
    # Is this file represented by a stub file (.pyi)?
    is_stub = False
    # Do weak typing globally in the file?
    weak_opts = None  # type: Set[str]

    def __init__(self,
                 defs: List[Node],
                 imports: List['ImportBase'],
                 is_bom: bool = False,
                 ignored_lines: Set[int] = None,
                 weak_opts: Set[str] = None) -> None:
        self.defs = defs
        self.line = 1  # Dummy line number
        self.imports = imports
        self.is_bom = is_bom
        self.weak_opts = weak_opts
        if ignored_lines:
            self.ignored_lines = ignored_lines
        else:
            self.ignored_lines = set()

    def name(self) -> str:
        return self._name

    def fullname(self) -> str:
        return self._fullname

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_mypy_file(self)

    def is_package_init_file(self) -> bool:
        return not (self.path is None) and len(self.path) != 0 \
            and os.path.basename(self.path).startswith('__init__.')


class ImportBase(Node):
    """Base class for all import statements."""
    is_unreachable = False


class Import(ImportBase):
    """import m [as n]"""

    ids = None  # type: List[Tuple[str, Optional[str]]]     # (module id, as id)

    def __init__(self, ids: List[Tuple[str, Optional[str]]]) -> None:
        self.ids = ids

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_import(self)


class ImportFrom(ImportBase):
    """from m import x [as y], ..."""

    names = None  # type: List[Tuple[str, Optional[str]]]  # Tuples (name, as name)

    def __init__(self, id: str, relative: int, names: List[Tuple[str, Optional[str]]]) -> None:
        self.id = id
        self.names = names
        self.relative = relative

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_import_from(self)


class ImportAll(ImportBase):
    """from m import *"""

    def __init__(self, id: str, relative: int) -> None:
        self.id = id
        self.relative = relative

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_import_all(self)


class FuncBase(SymbolNode):
    """Abstract base class for function-like nodes"""

    # Type signature. This is usually CallableType or Overloaded, but it can be something else for
    # decorated functions/
    type = None  # type: mypy.types.Type
    # If method, reference to TypeInfo
    info = None  # type: TypeInfo
    is_property = False

    @abstractmethod
    def name(self) -> str: pass

    def fullname(self) -> str:
        return self.name()

    def is_method(self) -> bool:
        return bool(self.info)


class OverloadedFuncDef(FuncBase):
    """A logical node representing all the variants of an overloaded function.

    This node has no explicit representation in the source program.
    Overloaded variants must be consecutive in the source file.
    """

    items = None  # type: List[Decorator]
    _fullname = None  # type: str

    def __init__(self, items: List['Decorator']) -> None:
        self.items = items
        self.set_line(items[0].line)

    def name(self) -> str:
        return self.items[1].func.name()

    def fullname(self) -> str:
        return self._fullname

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_overloaded_func_def(self)


class Argument(Node):
    """A single argument in a FuncItem.
    """
    def __init__(self, variable: 'Var', type_annotation: 'Optional[mypy.types.Type]',
            initializer: Optional[Node], kind: int,
            initialization_statement: Optional['AssignmentStmt'] = None) -> None:
        self.variable = variable

        self.type_annotation = type_annotation
        self.initializer = initializer

        self.initialization_statement = initialization_statement
        if not self.initialization_statement:
            self.initialization_statement = self._initialization_statement()

        self.kind = kind

    def _initialization_statement(self) -> Optional['AssignmentStmt']:
        """Convert the initializer into an assignment statement.
        """
        if not self.initializer:
            return None

        rvalue = self.initializer
        lvalue = NameExpr(self.variable.name())
        assign = AssignmentStmt([lvalue], rvalue)
        return assign

    def set_line(self, target: Union[Token, int]) -> Node:
        super().set_line(target)

        if self.initializer:
            self.initializer.set_line(self.line)

        self.variable.set_line(self.line)

        if self.initialization_statement:
            self.initialization_statement.set_line(self.line)
            self.initialization_statement.lvalues[0].set_line(self.line)


class FuncItem(FuncBase):
    arguments = []  # type: List[Argument]
    # Minimum number of arguments
    min_args = 0
    # Maximum number of positional arguments, -1 if no explicit limit (*args not included)
    max_pos = 0
    body = None  # type: Block
    is_implicit = False    # Implicit dynamic types?
    # Is this an overload variant of function with more than one overload variant?
    is_overload = False
    is_generator = False   # Contains a yield statement?
    is_coroutine = False   # Contains @coroutine or yield from Future
    is_static = False      # Uses @staticmethod?
    is_class = False       # Uses @classmethod?
    # Variants of function with type variables with values expanded
    expanded = None  # type: List[FuncItem]

    def __init__(self, arguments: List[Argument], body: 'Block',
                 typ: 'mypy.types.FunctionLike' = None) -> None:
        self.arguments = arguments
        arg_kinds = [arg.kind for arg in self.arguments]
        self.max_pos = arg_kinds.count(ARG_POS) + arg_kinds.count(ARG_OPT)
        self.body = body
        self.type = typ
        self.expanded = []

        self.min_args = 0
        for i in range(len(self.arguments)):
            if self.arguments[i] is None and i < self.max_fixed_argc():
                self.min_args = i + 1

    def max_fixed_argc(self) -> int:
        return self.max_pos

    def set_line(self, target: Union[Token, int]) -> Node:
        super().set_line(target)
        for arg in self.arguments:
            arg.set_line(self.line)
        return self

    def is_dynamic(self):
        return self.type is None


class FuncDef(FuncItem):
    """Function definition.

    This is a non-lambda function defined using 'def'.
    """

    _fullname = None  # type: str       # Name with module prefix
    is_decorated = False
    is_conditional = False             # Defined conditionally (within block)?
    is_abstract = False
    is_property = False
    original_def = None  # type: FuncDef  # Original conditional definition

    def __init__(self,
                 name: str,              # Function name
                 arguments: List[Argument],
                 body: 'Block',
                 typ: 'mypy.types.FunctionLike' = None) -> None:
        super().__init__(arguments, body, typ)
        self._name = name

    def name(self) -> str:
        return self._name

    def fullname(self) -> str:
        return self._fullname

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_func_def(self)

    def is_constructor(self) -> bool:
        return self.info is not None and self._name == '__init__'


class Decorator(SymbolNode):
    """A decorated function.

    A single Decorator object can include any number of function decorators.
    """

    func = None  # type: FuncDef           # Decorated function
    decorators = None  # type: List[Node]  # Decorators, at least one
    var = None  # type: Var              # Represents the decorated function obj
    is_overload = False

    def __init__(self, func: FuncDef, decorators: List[Node],
                 var: 'Var') -> None:
        self.func = func
        self.decorators = decorators
        self.var = var
        self.is_overload = False

    def name(self) -> str:
        return self.func.name()

    def fullname(self) -> str:
        return self.func.fullname()

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_decorator(self)


class Var(SymbolNode):
    """A variable.

    It can refer to global/local variable or a data attribute.
    """

    _name = None      # type: str   # Name without module prefix
    _fullname = None  # type: str   # Name with module prefix
    info = None  # type: TypeInfo   # Defining class (for member variables)
    type = None  # type: mypy.types.Type # Declared or inferred type, or None
    # Is this the first argument to an ordinary method (usually "self")?
    is_self = False
    is_ready = False  # If inferred, is the inferred type available?
    # Is this initialized explicitly to a non-None value in class body?
    is_initialized_in_class = False
    is_staticmethod = False
    is_classmethod = False
    is_property = False
    is_settable_property = False

    def __init__(self, name: str, type: 'mypy.types.Type' = None) -> None:
        self._name = name
        self.type = type
        self.is_self = False
        self.is_ready = True
        self.is_initialized_in_class = False

    def name(self) -> str:
        return self._name

    def fullname(self) -> str:
        return self._fullname

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_var(self)


class ClassDef(Node):
    """Class definition"""

    name = None  # type: str       # Name of the class without module prefix
    fullname = None  # type: str   # Fully qualified name of the class
    defs = None  # type: Block
    type_vars = None  # type: List[mypy.types.TypeVarDef]
    # Base class expressions (not semantically analyzed -- can be arbitrary expressions)
    base_type_exprs = None  # type: List[Node]
    # Semantically analyzed base types, derived from base_type_exprs during semantic analysis
    base_types = None  # type: List[mypy.types.Instance]
    info = None  # type: TypeInfo  # Related TypeInfo
    metaclass = ''
    decorators = None  # type: List[Node]
    # Built-in/extension class? (single implementation inheritance only)
    is_builtinclass = False

    def __init__(self, name: str, defs: 'Block',
                 type_vars: List['mypy.types.TypeVarDef'] = None,
                 base_type_exprs: List[Node] = None,
                 metaclass: str = None) -> None:
        if not base_type_exprs:
            base_type_exprs = []
        self.name = name
        self.defs = defs
        self.type_vars = type_vars or []
        self.base_type_exprs = base_type_exprs
        self.base_types = []  # Not yet semantically analyzed --> don't know base types
        self.metaclass = metaclass
        self.decorators = []

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_class_def(self)

    def is_generic(self) -> bool:
        return self.info.is_generic()


class GlobalDecl(Node):
    """Declaration global x, y, ..."""

    names = None  # type: List[str]

    def __init__(self, names: List[str]) -> None:
        self.names = names

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_global_decl(self)


class NonlocalDecl(Node):
    """Declaration nonlocal x, y, ..."""

    names = None  # type: List[str]

    def __init__(self, names: List[str]) -> None:
        self.names = names

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_nonlocal_decl(self)


class Block(Node):
    body = None  # type: List[Node]
    # True if we can determine that this block is not executed. For example,
    # this applies to blocks that are protected by something like "if PY3:"
    # when using Python 2.
    is_unreachable = False

    def __init__(self, body: List[Node]) -> None:
        self.body = body

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_block(self)


# Statements


class ExpressionStmt(Node):
    """An expression as a statament, such as print(s)."""
    expr = None  # type: Node

    def __init__(self, expr: Node) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_expression_stmt(self)


class AssignmentStmt(Node):
    """Assignment statement

    The same node class is used for single assignment, multiple assignment
    (e.g. x, y = z) and chained assignment (e.g. x = y = z), assignments
    that define new names, and assignments with explicit types (# type).

    An lvalue can be NameExpr, TupleExpr, ListExpr, MemberExpr, IndexExpr.
    """

    lvalues = None  # type: List[Node]
    rvalue = None  # type: Node
    # Declared type in a comment, may be None.
    type = None  # type: mypy.types.Type

    def __init__(self, lvalues: List[Node], rvalue: Node,
                 type: 'mypy.types.Type' = None) -> None:
        self.lvalues = lvalues
        self.rvalue = rvalue
        self.type = type

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_assignment_stmt(self)


class OperatorAssignmentStmt(Node):
    """Operator assignment statement such as x += 1"""

    op = ''
    lvalue = None  # type: Node
    rvalue = None  # type: Node

    def __init__(self, op: str, lvalue: Node, rvalue: Node) -> None:
        self.op = op
        self.lvalue = lvalue
        self.rvalue = rvalue

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_operator_assignment_stmt(self)


class WhileStmt(Node):
    expr = None  # type: Node
    body = None  # type: Block
    else_body = None  # type: Block

    def __init__(self, expr: Node, body: Block, else_body: Block) -> None:
        self.expr = expr
        self.body = body
        self.else_body = else_body

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_while_stmt(self)


class ForStmt(Node):
    # Index variables
    index = None  # type: Node
    # Expression to iterate
    expr = None  # type: Node
    body = None  # type: Block
    else_body = None  # type: Block

    def __init__(self, index: Node, expr: Node, body: Block,
                 else_body: Block) -> None:
        self.index = index
        self.expr = expr
        self.body = body
        self.else_body = else_body

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_for_stmt(self)


class ReturnStmt(Node):
    expr = None  # type: Node   # Expression or None

    def __init__(self, expr: Node) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_return_stmt(self)


class AssertStmt(Node):
    expr = None  # type: Node

    def __init__(self, expr: Node) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_assert_stmt(self)


class YieldStmt(Node):
    expr = None  # type: Node

    def __init__(self, expr: Node) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_yield_stmt(self)


class YieldFromStmt(Node):
    expr = None  # type: Node

    def __init__(self, expr: Node) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_yield_from_stmt(self)


class DelStmt(Node):
    expr = None  # type: Node

    def __init__(self, expr: Node) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_del_stmt(self)


class BreakStmt(Node):
    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_break_stmt(self)


class ContinueStmt(Node):
    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_continue_stmt(self)


class PassStmt(Node):
    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_pass_stmt(self)


class IfStmt(Node):
    expr = None  # type: List[Node]
    body = None  # type: List[Block]
    else_body = None  # type: Block

    def __init__(self, expr: List[Node], body: List[Block],
                 else_body: Block) -> None:
        self.expr = expr
        self.body = body
        self.else_body = else_body

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_if_stmt(self)


class RaiseStmt(Node):
    expr = None  # type: Node
    from_expr = None  # type: Node

    def __init__(self, expr: Node, from_expr: Node = None) -> None:
        self.expr = expr
        self.from_expr = from_expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_raise_stmt(self)


class TryStmt(Node):
    body = None  # type: Block                # Try body
    types = None  # type: List[Node]          # Except type expressions
    vars = None  # type: List[NameExpr]     # Except variable names
    handlers = None  # type: List[Block]      # Except bodies
    else_body = None  # type: Block
    finally_body = None  # type: Block

    def __init__(self, body: Block, vars: List['NameExpr'], types: List[Node],
                 handlers: List[Block], else_body: Block,
                 finally_body: Block) -> None:
        self.body = body
        self.vars = vars
        self.types = types
        self.handlers = handlers
        self.else_body = else_body
        self.finally_body = finally_body

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_try_stmt(self)


class WithStmt(Node):
    expr = None  # type: List[Node]
    target = None  # type: List[Node]
    body = None  # type: Block

    def __init__(self, expr: List[Node], target: List[Node],
                 body: Block) -> None:
        self.expr = expr
        self.target = target
        self.body = body

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_with_stmt(self)


class PrintStmt(Node):
    """Python 2 print statement"""

    args = None  # type: List[Node]
    newline = False
    # The file-like target object (given using >>).
    target = None  # type: Optional[Node]

    def __init__(self, args: List[Node], newline: bool, target: Node = None) -> None:
        self.args = args
        self.newline = newline
        self.target = target

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_print_stmt(self)


class ExecStmt(Node):
    """Python 2 exec statement"""

    expr = None  # type: Node
    variables1 = None  # type: Optional[Node]
    variables2 = None  # type: Optional[Node]

    def __init__(self, expr: Node, variables1: Optional[Node], variables2: Optional[Node]) -> None:
        self.expr = expr
        self.variables1 = variables1
        self.variables2 = variables2

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_exec_stmt(self)


# Expressions


class IntExpr(Node):
    """Integer literal"""

    value = 0
    literal = LITERAL_YES

    def __init__(self, value: int) -> None:
        self.value = value
        self.literal_hash = value

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_int_expr(self)


class StrExpr(Node):
    """String literal"""

    value = ''
    literal = LITERAL_YES

    def __init__(self, value: str) -> None:
        self.value = value
        self.literal_hash = value

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_str_expr(self)


class BytesExpr(Node):
    """Bytes literal"""

    value = ''  # TODO use bytes
    literal = LITERAL_YES

    def __init__(self, value: str) -> None:
        self.value = value
        self.literal_hash = value

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_bytes_expr(self)


class UnicodeExpr(Node):
    """Unicode literal (Python 2.x)"""

    value = ''  # TODO use bytes
    literal = LITERAL_YES

    def __init__(self, value: str) -> None:
        self.value = value
        self.literal_hash = value

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_unicode_expr(self)


class FloatExpr(Node):
    """Float literal"""

    value = 0.0
    literal = LITERAL_YES

    def __init__(self, value: float) -> None:
        self.value = value
        self.literal_hash = value

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_float_expr(self)


class ComplexExpr(Node):
    """Complex literal"""

    value = 0.0j
    literal = LITERAL_YES

    def __init__(self, value: complex) -> None:
        self.value = value
        self.literal_hash = value

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_complex_expr(self)


class EllipsisExpr(Node):
    """Ellipsis (...)"""

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_ellipsis(self)


class StarExpr(Node):
    """Star expression"""

    expr = None  # type: Node

    def __init__(self, expr: Node) -> None:
        self.expr = expr
        self.literal = self.expr.literal
        self.literal_hash = ('Star', expr.literal_hash,)

        # Whether this starred expression is used in a tuple/list and as lvalue
        self.valid = False

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_star_expr(self)


class RefExpr(Node):
    """Abstract base class for name-like constructs"""

    kind = None  # type: int      # LDEF/GDEF/MDEF/... (None if not available)
    node = None  # type: Node        # Var, FuncDef or TypeInfo that describes this
    fullname = None  # type: str  # Fully qualified name (or name if not global)

    # Does this define a new name with inferred type?
    #
    # For members, after semantic analysis, this does not take base
    # classes into consideration at all; the type checker deals with these.
    is_def = False


class NameExpr(RefExpr):
    """Name expression

    This refers to a local name, global name or a module.
    """

    name = None  # type: str      # Name referred to (may be qualified)
    # TypeInfo of class surrounding expression (may be None)
    info = None  # type: TypeInfo

    literal = LITERAL_TYPE

    def __init__(self, name: str) -> None:
        self.name = name
        self.literal_hash = ('Var', name,)

    def type_node(self):
        return cast(TypeInfo, self.node)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_name_expr(self)


class MemberExpr(RefExpr):
    """Member access expression x.y"""

    expr = None  # type: Node
    name = None  # type: str
    # The variable node related to a definition.
    def_var = None  # type: Var

    def __init__(self, expr: Node, name: str) -> None:
        self.expr = expr
        self.name = name
        self.literal = self.expr.literal
        self.literal_hash = ('Member', expr.literal_hash, name)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_member_expr(self)


# Kinds of arguments

# Positional argument
ARG_POS = 0  # type: int
# Positional, optional argument (functions only, not calls)
ARG_OPT = 1  # type: int
# *arg argument
ARG_STAR = 2  # type: int
# Keyword argument x=y in call, or keyword-only function arg
ARG_NAMED = 3  # type: int
# **arg argument
ARG_STAR2 = 4  # type: int


class CallExpr(Node):
    """Call expression.

    This can also represent several special forms that are syntactically calls
    such as cast(...) and None  # type: ....
    """

    callee = None  # type: Node
    args = None  # type: List[Node]
    arg_kinds = None  # type: List[int]  # ARG_ constants
    # Each name can be None if not a keyword argument.
    arg_names = None  # type: List[str]
    # If not None, the node that represents the meaning of the CallExpr. For
    # cast(...) this is a CastExpr.
    analyzed = None  # type: Node

    def __init__(self, callee: Node, args: List[Node], arg_kinds: List[int],
                 arg_names: List[str] = None, analyzed: Node = None) -> None:
        if not arg_names:
            arg_names = [None] * len(args)
        self.callee = callee
        self.args = args
        self.arg_kinds = arg_kinds
        self.arg_names = arg_names
        self.analyzed = analyzed

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_call_expr(self)


class YieldFromExpr(Node):
    expr = None  # type: Node

    def __init__(self, expr: Node) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_yield_from_expr(self)


class YieldExpr(Node):
    expr = None  # type: Optional[Node]

    def __init__(self, expr: Optional[Node]) -> None:
        self.expr = expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_yield_expr(self)


class IndexExpr(Node):
    """Index expression x[y].

    Also wraps type application such as List[int] as a special form.
    """

    base = None  # type: Node
    index = None  # type: Node
    # Inferred __getitem__ method type
    method_type = None  # type: mypy.types.Type
    # If not None, this is actually semantically a type application
    # Class[type, ...] or a type alias initializer.
    analyzed = None  # type: Union[TypeApplication, TypeAliasExpr]

    def __init__(self, base: Node, index: Node) -> None:
        self.base = base
        self.index = index
        self.analyzed = None
        if self.index.literal == LITERAL_YES:
            self.literal = self.base.literal
            self.literal_hash = ('Member', base.literal_hash,
                                 index.literal_hash)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_index_expr(self)


class UnaryExpr(Node):
    """Unary operation"""

    op = ''
    expr = None  # type: Node
    # Inferred operator method type
    method_type = None  # type: mypy.types.Type

    def __init__(self, op: str, expr: Node) -> None:
        self.op = op
        self.expr = expr
        self.literal = self.expr.literal
        self.literal_hash = ('Unary', op, expr.literal_hash)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_unary_expr(self)


# Map from binary operator id to related method name (in Python 3).
op_methods = {
    '+': '__add__',
    '-': '__sub__',
    '*': '__mul__',
    '/': '__truediv__',
    '%': '__mod__',
    '//': '__floordiv__',
    '**': '__pow__',
    '&': '__and__',
    '|': '__or__',
    '^': '__xor__',
    '<<': '__lshift__',
    '>>': '__rshift__',
    '==': '__eq__',
    '!=': '__ne__',
    '<': '__lt__',
    '>=': '__ge__',
    '>': '__gt__',
    '<=': '__le__',
    'in': '__contains__',
}  # type: Dict[str, str]

ops_with_inplace_method = {
    '+', '-', '*', '/', '%', '//', '**', '&', '|', '^', '<<', '>>'}

inplace_operator_methods = set(
    '__i' + op_methods[op][2:] for op in ops_with_inplace_method)

reverse_op_methods = {
    '__add__': '__radd__',
    '__sub__': '__rsub__',
    '__mul__': '__rmul__',
    '__truediv__': '__rtruediv__',
    '__mod__': '__rmod__',
    '__floordiv__': '__rfloordiv__',
    '__pow__': '__rpow__',
    '__and__': '__rand__',
    '__or__': '__ror__',
    '__xor__': '__rxor__',
    '__lshift__': '__rlshift__',
    '__rshift__': '__rrshift__',
    '__eq__': '__eq__',
    '__ne__': '__ne__',
    '__lt__': '__gt__',
    '__ge__': '__le__',
    '__gt__': '__lt__',
    '__le__': '__ge__',
}

normal_from_reverse_op = dict((m, n) for n, m in reverse_op_methods.items())
reverse_op_method_set = set(reverse_op_methods.values())


class OpExpr(Node):
    """Binary operation (other than . or [] or comparison operators,
    which have specific nodes)."""

    op = ''
    left = None  # type: Node
    right = None  # type: Node
    # Inferred type for the operator method type (when relevant).
    method_type = None  # type: mypy.types.Type

    def __init__(self, op: str, left: Node, right: Node) -> None:
        self.op = op
        self.left = left
        self.right = right
        self.literal = min(self.left.literal, self.right.literal)
        self.literal_hash = ('Binary', op, left.literal_hash, right.literal_hash)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_op_expr(self)


class ComparisonExpr(Node):
    """Comparison expression (e.g. a < b > c < d)."""

    operators = None  # type: List[str]
    operands = None  # type: List[Node]
    # Inferred type for the operator methods (when relevant; None for 'is').
    method_types = None  # type: List[mypy.types.Type]

    def __init__(self, operators: List[str], operands: List[Node]) -> None:
        self.operators = operators
        self.operands = operands
        self.method_types = []
        self.literal = min(o.literal for o in self.operands)
        self.literal_hash = (('Comparison',) + tuple(operators) +
                             tuple(o.literal_hash for o in operands))

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_comparison_expr(self)


class SliceExpr(Node):
    """Slice expression (e.g. 'x:y', 'x:', '::2' or ':').

    This is only valid as index in index expressions.
    """

    begin_index = None  # type: Node  # May be None
    end_index = None  # type: Node    # May be None
    stride = None  # type: Node       # May be None

    def __init__(self, begin_index: Node, end_index: Node,
                 stride: Node) -> None:
        self.begin_index = begin_index
        self.end_index = end_index
        self.stride = stride

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_slice_expr(self)


class CastExpr(Node):
    """Cast expression cast(type, expr)."""

    expr = None  # type: Node
    type = None  # type: mypy.types.Type

    def __init__(self, expr: Node, typ: 'mypy.types.Type') -> None:
        self.expr = expr
        self.type = typ

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_cast_expr(self)


class SuperExpr(Node):
    """Expression super().name"""

    name = ''
    info = None  # type: TypeInfo  # Type that contains this super expression

    def __init__(self, name: str) -> None:
        self.name = name

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_super_expr(self)


class FuncExpr(FuncItem):
    """Lambda expression"""

    def name(self) -> str:
        return '<lambda>'

    def expr(self) -> Node:
        """Return the expression (the body) of the lambda."""
        ret = cast(ReturnStmt, self.body.body[0])
        return ret.expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_func_expr(self)


class ListExpr(Node):
    """List literal expression [...]."""

    items = None  # type: List[Node]

    def __init__(self, items: List[Node]) -> None:
        self.items = items
        if all(x.literal == LITERAL_YES for x in items):
            self.literal = LITERAL_YES
            self.literal_hash = ('List',) + tuple(x.literal_hash for x in items)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_list_expr(self)


class DictExpr(Node):
    """Dictionary literal expression {key: value, ...}."""

    items = None  # type: List[Tuple[Node, Node]]

    def __init__(self, items: List[Tuple[Node, Node]]) -> None:
        self.items = items
        if all(x[0].literal == LITERAL_YES and x[1].literal == LITERAL_YES
               for x in items):
            self.literal = LITERAL_YES
            self.literal_hash = ('Dict',) + tuple((x[0].literal_hash, x[1].literal_hash)
                                                  for x in items)  # type: ignore

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_dict_expr(self)


class TupleExpr(Node):
    """Tuple literal expression (..., ...)"""

    items = None  # type: List[Node]

    def __init__(self, items: List[Node]) -> None:
        self.items = items
        if all(x.literal == LITERAL_YES for x in items):
            self.literal = LITERAL_YES
            self.literal_hash = ('Tuple',) + tuple(x.literal_hash for x in items)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_tuple_expr(self)


class SetExpr(Node):
    """Set literal expression {value, ...}."""

    items = None  # type: List[Node]

    def __init__(self, items: List[Node]) -> None:
        self.items = items
        if all(x.literal == LITERAL_YES for x in items):
            self.literal = LITERAL_YES
            self.literal_hash = ('Set',) + tuple(x.literal_hash for x in items)

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_set_expr(self)


class GeneratorExpr(Node):
    """Generator expression ... for ... in ... [ for ...  in ... ] [ if ... ]."""

    left_expr = None  # type: Node
    sequences_expr = None  # type: List[Node]
    condlists = None  # type: List[List[Node]]
    indices = None  # type: List[Node]

    def __init__(self, left_expr: Node, indices: List[Node],
                 sequences: List[Node], condlists: List[List[Node]]) -> None:
        self.left_expr = left_expr
        self.sequences = sequences
        self.condlists = condlists
        self.indices = indices

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_generator_expr(self)


class ListComprehension(Node):
    """List comprehension (e.g. [x + 1 for x in a])"""

    generator = None  # type: GeneratorExpr

    def __init__(self, generator: GeneratorExpr) -> None:
        self.generator = generator

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_list_comprehension(self)


class SetComprehension(Node):
    """Set comprehension (e.g. {x + 1 for x in a})"""

    generator = None  # type: GeneratorExpr

    def __init__(self, generator: GeneratorExpr) -> None:
        self.generator = generator

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_set_comprehension(self)


class DictionaryComprehension(Node):
    """Dictionary comprehension (e.g. {k: v for k, v in a}"""

    key = None  # type: Node
    value = None  # type: Node
    sequences_expr = None  # type: List[Node]
    condlists = None  # type: List[List[Node]]
    indices = None  # type: List[Node]

    def __init__(self, key: Node, value: Node, indices: List[Node],
                 sequences: List[Node], condlists: List[List[Node]]) -> None:
        self.key = key
        self.value = value
        self.sequences = sequences
        self.condlists = condlists
        self.indices = indices

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_dictionary_comprehension(self)


class ConditionalExpr(Node):
    """Conditional expression (e.g. x if y else z)"""

    cond = None  # type: Node
    if_expr = None  # type: Node
    else_expr = None  # type: Node

    def __init__(self, cond: Node, if_expr: Node, else_expr: Node) -> None:
        self.cond = cond
        self.if_expr = if_expr
        self.else_expr = else_expr

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_conditional_expr(self)


class TypeApplication(Node):
    """Type application expr[type, ...]"""

    expr = None  # type: Node
    types = None  # type: List[mypy.types.Type]

    def __init__(self, expr: Node, types: List['mypy.types.Type']) -> None:
        self.expr = expr
        self.types = types

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_type_application(self)


# Variance of a type variable. For example, T in the definition of
# List[T] is invariant, so List[int] is not a subtype of List[object],
# and also List[object] is not a subtype of List[int].
#
# The T in Iterable[T] is covariant, so Iterable[int] is a subtype of
# Iterable[object], but not vice versa.
#
# If T is contravariant in Foo[T], Foo[object] is a subtype of
# Foo[int], but not vice versa.
INVARIANT = 0  # type: int
COVARIANT = 1  # type: int
CONTRAVARIANT = 2  # type: int


class TypeVarExpr(SymbolNode):
    """Type variable expression TypeVar(...)."""

    _name = ''
    _fullname = ''
    # Value restriction: only types in the list are valid as values. If the
    # list is empty, there is no restriction.
    values = None  # type: List[mypy.types.Type]
    # Variance of the type variable. Invariant is the default.
    # TypeVar(..., covariant=True) defines a covariant type variable.
    # TypeVar(..., contravariant=True) defines a contravariant type
    # variable.
    variance = INVARIANT

    def __init__(self, name: str, fullname: str,
                 values: List['mypy.types.Type'],
                 variance: int=INVARIANT) -> None:
        self._name = name
        self._fullname = fullname
        self.values = values
        self.variance = variance

    def name(self) -> str:
        return self._name

    def fullname(self) -> str:
        return self._fullname

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_type_var_expr(self)


class TypeAliasExpr(Node):
    """Type alias expression (rvalue)."""

    type = None  # type: mypy.types.Type

    def __init__(self, type: 'mypy.types.Type') -> None:
        self.type = type

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_type_alias_expr(self)


class NamedTupleExpr(Node):
    """Named tuple expression namedtuple(...)."""

    # The class representation of this named tuple (its tuple_type attribute contains
    # the tuple item types)
    info = None  # type: TypeInfo

    def __init__(self, info: 'TypeInfo') -> None:
        self.info = info

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_namedtuple_expr(self)


class PromoteExpr(Node):
    """Ducktype class decorator expression _promote(...)."""

    type = None  # type: mypy.types.Type

    def __init__(self, type: 'mypy.types.Type') -> None:
        self.type = type

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit__promote_expr(self)


# Constants


class TempNode(Node):
    """Temporary dummy node used during type checking.

    This node is not present in the original program; it is just an artifact
    of the type checker implementation. It only represents an opaque node with
    some fixed type.
    """

    type = None  # type: mypy.types.Type

    def __init__(self, typ: 'mypy.types.Type') -> None:
        self.type = typ

    def accept(self, visitor: NodeVisitor[T]) -> T:
        return visitor.visit_temp_node(self)


class TypeInfo(SymbolNode):
    """Class representing the type structure of a single class.

    The corresponding ClassDef instance represents the AST of
    the class.
    """

    _fullname = None  # type: str          # Fully qualified name
    defn = None  # type: ClassDef          # Corresponding ClassDef
    # Method Resolution Order: the order of looking up attributes. The first
    # value always to refers to this class.
    mro = None  # type: List[TypeInfo]
    subtypes = None  # type: Set[TypeInfo] # Direct subclasses encountered so far
    names = None  # type: SymbolTable      # Names defined directly in this type
    is_abstract = False                    # Does the class have any abstract attributes?
    abstract_attributes = None  # type: List[str]
    is_enum = False
    # If true, any unknown attributes should have type 'Any' instead
    # of generating a type error.  This would be true if there is a
    # base class with type 'Any', but other use cases may be
    # possible. This is similar to having __getattr__ that returns Any
    # (and __setattr__), but without the __getattr__ method.
    fallback_to_any = False

    # Information related to type annotations.

    # Generic type variable names
    type_vars = None  # type: List[str]

    # Direct base classes.
    bases = None  # type: List[mypy.types.Instance]

    # Duck type compatibility (_promote decorator)
    _promote = None  # type: mypy.types.Type

    # Representation of a Tuple[...] base class, if the class has any
    # (e.g., for named tuples). If this is not None, the actual Type
    # object used for this class is not an Instance but a TupleType;
    # the corresponding Instance is set as the fallback type of the
    # tuple type.
    tuple_type = None  # type: mypy.types.TupleType

    # Is this a named tuple type?
    is_named_tuple = False

    def __init__(self, names: 'SymbolTable', defn: ClassDef) -> None:
        """Initialize a TypeInfo."""
        self.names = names
        self.defn = defn
        self.subtypes = set()
        self.mro = []
        self.type_vars = []
        self.bases = []
        self._fullname = defn.fullname
        self.is_abstract = False
        self.abstract_attributes = []
        if defn.type_vars:
            for vd in defn.type_vars:
                self.type_vars.append(vd.name)

    def name(self) -> str:
        """Short name."""
        return self.defn.name

    def fullname(self) -> str:
        return self._fullname

    def is_generic(self) -> bool:
        """Is the type generic (i.e. does it have type variables)?"""
        return self.type_vars is not None and len(self.type_vars) > 0

    def get(self, name: str) -> 'SymbolTableNode':
        for cls in self.mro:
            n = cls.names.get(name)
            if n:
                return n
        return None

    def __getitem__(self, name: str) -> 'SymbolTableNode':
        n = self.get(name)
        if n:
            return n
        else:
            raise KeyError(name)

    def __repr__(self) -> str:
        return '<TypeInfo %s>' % self.fullname()

    # IDEA: Refactor the has* methods to be more consistent and document
    #       them.

    def has_readable_member(self, name: str) -> bool:
        return self.get(name) is not None

    def has_writable_member(self, name: str) -> bool:
        return self.has_var(name)

    def has_var(self, name: str) -> bool:
        return self.get_var(name) is not None

    def has_method(self, name: str) -> bool:
        return self.get_method(name) is not None

    def get_var(self, name: str) -> Var:
        for cls in self.mro:
            if name in cls.names:
                node = cls.names[name].node
                if isinstance(node, Var):
                    return cast(Var, node)
                else:
                    return None
        return None

    def get_var_or_getter(self, name: str) -> SymbolNode:
        # TODO getter
        return self.get_var(name)

    def get_var_or_setter(self, name: str) -> SymbolNode:
        # TODO setter
        return self.get_var(name)

    def get_method(self, name: str) -> FuncBase:
        for cls in self.mro:
            if name in cls.names:
                node = cls.names[name].node
                if isinstance(node, FuncBase):
                    return node
                else:
                    return None
        return None

    def calculate_mro(self) -> None:
        """Calculate and set mro (method resolution order).

        Raise MroError if cannot determine mro.
        """
        self.mro = linearize_hierarchy(self)

    def has_base(self, fullname: str) -> bool:
        """Return True if type has a base type with the specified name.

        This can be either via extension or via implementation.
        """
        for cls in self.mro:
            if cls.fullname() == fullname:
                return True
        return False

    def all_subtypes(self) -> 'Set[TypeInfo]':
        """Return TypeInfos of all subtypes, including this type, as a set."""
        subtypes = set([self])
        for subt in self.subtypes:
            for t in subt.all_subtypes():
                subtypes.add(t)
        return subtypes

    def all_base_classes(self) -> 'List[TypeInfo]':
        """Return a list of base classes, including indirect bases."""
        assert False

    def direct_base_classes(self) -> 'List[TypeInfo]':
        """Return a direct base classes.

        Omit base classes of other base classes.
        """
        return [base.type for base in self.bases]

    def __str__(self) -> str:
        """Return a string representation of the type.

        This includes the most important information about the type.
        """
        base = None  # type: str
        if self.bases:
            base = 'Bases({})'.format(', '.join(str(base)
                                                for base in self.bases))
        return dump_tagged(['Name({})'.format(self.fullname()),
                            base,
                            ('Names', sorted(self.names.keys()))],
                           'TypeInfo')


class SymbolTableNode:
    # LDEF/GDEF/MDEF/UNBOUND_TVAR/TVAR/...
    kind = None  # type: int
    # AST node of definition (FuncDef/Var/TypeInfo/Decorator/TypeVarExpr,
    # or None for a bound type variable).
    node = None  # type: SymbolNode
    # Type variable id (for bound type variables only)
    tvar_id = 0
    # Module id (e.g. "foo.bar") or None
    mod_id = ''
    # If this not None, override the type of the 'node' attribute.
    type_override = None  # type: mypy.types.Type
    # If False, this name won't be imported via 'from <module> import *'.
    # This has no effect on names within classes.
    module_public = True

    def __init__(self, kind: int, node: SymbolNode, mod_id: str = None,
                 typ: 'mypy.types.Type' = None, tvar_id: int = 0,
                 module_public: bool = True) -> None:
        self.kind = kind
        self.node = node
        self.type_override = typ
        self.mod_id = mod_id
        self.tvar_id = tvar_id
        self.module_public = module_public

    @property
    def fullname(self) -> str:
        if self.node is not None:
            return self.node.fullname()
        else:
            return None

    @property
    def type(self) -> 'mypy.types.Type':
        # IDEA: Get rid of the Any type.
        node = self.node  # type: Any
        if self.type_override is not None:
            return self.type_override
        elif ((isinstance(node, Var) or isinstance(node, FuncDef))
              and node.type is not None):
            return node.type
        elif isinstance(node, Decorator):
            return (cast(Decorator, node)).var.type
        else:
            return None

    def __str__(self) -> str:
        s = '{}/{}'.format(node_kinds[self.kind], short_type(self.node))
        if self.mod_id is not None:
            s += ' ({})'.format(self.mod_id)
        # Include declared type of variables and functions.
        if self.type is not None:
            s += ' : {}'.format(self.type)
        return s


class SymbolTable(Dict[str, SymbolTableNode]):
    def __str__(self) -> str:
        a = []  # type: List[str]
        for key, value in self.items():
            # Filter out the implicit import of builtins.
            if isinstance(value, SymbolTableNode):
                if (value.fullname != 'builtins' and
                        value.fullname.split('.')[-1] not in
                        implicit_module_attrs):
                    a.append('  ' + str(key) + ' : ' + str(value))
            else:
                a.append('  <invalid item>')
        a = sorted(a)
        a.insert(0, 'SymbolTable(')
        a[-1] += ')'
        return '\n'.join(a)


def clean_up(s: str) -> str:
    # TODO remove
    return re.sub('.*::', '', s)


def function_type(func: FuncBase, fallback: 'mypy.types.Instance') -> 'mypy.types.FunctionLike':
    while not hasattr(func, 'type'):
        func = func.func
    if func.type:
        assert isinstance(func.type, mypy.types.FunctionLike)
        return cast(mypy.types.FunctionLike, func.type)
    else:
        # Implicit type signature with dynamic types.
        # Overloaded functions always have a signature, so func must be an ordinary function.
        fdef = cast(FuncDef, func)
        name = func.name()
        if name:
            name = '"{}"'.format(name)
        names = []  # type: List[str]
        for arg in fdef.arguments:
            names.append(arg.variable.name())

        return mypy.types.CallableType(
            [mypy.types.AnyType()] * len(fdef.arguments),
            [arg.kind for arg in fdef.arguments],
            names,
            mypy.types.AnyType(),
            fallback,
            name,
        )


def method_type_with_fallback(func: FuncBase,
                              fallback: 'mypy.types.Instance') -> 'mypy.types.FunctionLike':
    """Return the signature of a method (omit self)."""
    return method_type(function_type(func, fallback))


def method_type(sig: 'mypy.types.FunctionLike') -> 'mypy.types.FunctionLike':
    if isinstance(sig, mypy.types.CallableType):
        return method_callable(sig)
    else:
        sig = cast(mypy.types.Overloaded, sig)
        items = []  # type: List[mypy.types.CallableType]
        for c in sig.items():
            items.append(method_callable(c))
        return mypy.types.Overloaded(items)


def method_callable(c: 'mypy.types.CallableType') -> 'mypy.types.CallableType':
    return c.copy_modified(arg_types=c.arg_types[1:],
                           arg_kinds=c.arg_kinds[1:],
                           arg_names=c.arg_names[1:])


class MroError(Exception):
    """Raised if a consistent mro cannot be determined for a class."""


def linearize_hierarchy(info: TypeInfo) -> List[TypeInfo]:
    # TODO describe
    if info.mro:
        return info.mro
    bases = info.direct_base_classes()
    return [info] + merge([linearize_hierarchy(base) for base in bases] +
                          [bases])


def merge(seqs: List[List[TypeInfo]]) -> List[TypeInfo]:
    seqs = [s[:] for s in seqs]
    result = []  # type: List[TypeInfo]
    while True:
        seqs = [s for s in seqs if s]
        if not seqs:
            return result
        for seq in seqs:
            head = seq[0]
            if not [s for s in seqs if head in s[1:]]:
                break
        else:
            raise MroError()
        result.append(head)
        for s in seqs:
            if s[0] is head:
                del s[0]
