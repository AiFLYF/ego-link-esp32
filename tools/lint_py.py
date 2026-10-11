#!/usr/bin/env python
"""针对性的 Python 静态检查 —— 沙箱里装不上 pyflakes 时的替代。

只查**高信噪比**的四类问题（都是这个项目真踩过的）：

  1. **未定义的名字** —— 这个仓库真出过 `NameError: name 'os' is not defined`
     （手册 T49，只在 linked worktree 分支上炸），静态查一遍最省事。
  2. **没用到 / 只用到一半的 import** —— 噪音最小的清理项。
  3. **裸 `except:`** —— 会连 KeyboardInterrupt / SystemExit 一起吞掉。
  4. **可变默认参数**（`def f(x=[])`）—— 经典陷阱。

用法：
    python tools/lint_py.py            # 查 server/ 与 tools/ 下的 .py
    python tools/lint_py.py 文件.py ... # 只查指定文件

退出码：发现问题返回 1，干净返回 0（方便挂到门禁上）。
"""
import ast
import builtins
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKIP_DIRS = {"__pycache__", "build", "managed_components", ".git"}


def target_files(argv):
    if argv:
        return [os.path.abspath(a) for a in argv]
    out = []
    for base in (os.path.join(ROOT, "server"), os.path.join(ROOT, "tools")):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    out.append(os.path.join(dirpath, fn))
    return out


class Scope:
    """一层作用域：记下这层里"定义了哪些名字"以及"这些名字是不是 import 来的"。"""

    def __init__(self, parent=None):
        self.parent = parent
        self.names = set()
        self.imported = {}          # 名字 -> (行号, 是不是 from-import)

    def add(self, name):
        self.names.add(name)

    def has(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return True
            s = s.parent
        return False

    def imports(self):
        """把本层（含嵌套层）的 import 汇总出去，用于"有没有被用到"。"""
        out = []
        s = self
        while s is not None:
            out.extend(s.imported.items())
            s = s.parent
        return out


def collect_targets(node):
    """赋值/for/with/except 这些语句"定义了哪些名字"。"""
    names = []
    for t in ast.walk(node):
        if isinstance(t, ast.Name) and isinstance(t.ctx, (ast.Store, ast.Del)):
            names.append(t.id)
        elif isinstance(t, (ast.Import, ast.ImportFrom)):
            pass
    return names


class Checker(ast.NodeVisitor):
    def __init__(self, path, src):
        self.path = path
        self.src = src
        self.problems = []
        self.scope = Scope()
        self.loads = []             # (名字, 行号) —— 收集起来最后一起判
        self.builtin = set(dir(builtins)) | {
            # 模块级的内置名字，不在 dir(builtins) 里，但任何模块都能直接用
            "__file__", "__name__", "__doc__", "__package__",
            "__spec__", "__loader__", "__builtins__", "__debug__",
        }

    def report(self, line, kind, msg):
        self.problems.append((line, kind, msg))

    # ---- 作用域进入 / 退出 ------------------------------------------------
    def _enter(self):
        self.scope = Scope(self.scope)

    def _exit(self):
        self.scope = self.scope.parent

    def visit_Module(self, node):
        # 模块级：预扫一遍所有顶层定义（函数可以先互相引用）
        for t in node.body:
            for n in self._declared_names(t):
                self.scope.add(n)
        self.generic_visit(node)
        self._check_imports()

    def _declared_names(self, node):
        out = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                out += collect_targets(tgt)
        elif isinstance(node, ast.AnnAssign):
            out += collect_targets(node.target)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.name == "*":
                    continue
                out.append(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
            for sub in ast.iter_child_nodes(node):
                if isinstance(sub, ast.stmt):
                    out += self._declared_names(sub)
        return out

    def visit_Import(self, node):
        for a in node.names:
            name = a.asname or a.name.split(".")[0]
            self.scope.add(name)
            self.scope.imported[name] = (node.lineno, False)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name == "*":
                continue
            name = a.asname or a.name
            self.scope.add(name)
            # `from __future__ import ...` 是给编译器看的，有副作用、不是"没用到"
            if node.module != "__future__":
                self.scope.imported[name] = (node.lineno, True)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._func(node)

    def visit_AsyncFunctionDef(self, node):
        self._func(node)

    def _func(self, node):
        # 可变默认参数
        for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                self.report(d.lineno, "mutable-default",
                            "%s() 的默认参数是可变对象 —— 会被所有调用共享" % node.name)
        self._enter()
        for a in (list(node.args.posonlyargs) + list(node.args.args)
                  + list(node.args.kwonlyargs)):
            self.scope.add(a.arg)
        if node.args.vararg:
            self.scope.add(node.args.vararg.arg)
        if node.args.kwarg:
            self.scope.add(node.args.kwarg.arg)
        for t in node.body:
            for n in self._declared_names(t):
                self.scope.add(n)
        self.generic_visit(node)
        self._exit()

    def visit_Lambda(self, node):
        self._enter()
        for a in (list(node.args.posonlyargs) + list(node.args.args)
                  + list(node.args.kwonlyargs)):
            self.scope.add(a.arg)
        self.generic_visit(node)
        self._exit()

    def visit_ClassDef(self, node):
        self._enter()
        self.generic_visit(node)
        self._exit()

    def visit_ExceptHandler(self, node):
        if node.type is None:
            self.report(node.lineno, "bare-except",
                        "裸 except: 会把 KeyboardInterrupt / SystemExit 也吞掉")
        if node.name:
            self.scope.add(node.name)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.loads.append((node.id, node.lineno))
        self.generic_visit(node)

    # ---- 判定 -------------------------------------------------------------
    def _check_imports(self):
        used = {n for n, _ in self.loads}
        # 字符串里出现也算用到（`__all__ = ["x"]`、typing 前向引用等）
        for name, (line, _) in self.scope.imported.items():
            if name in used:
                continue
            if '"%s"' % name in self.src or "'%s'" % name in self.src:
                continue
            self.report(line, "unused-import", "import 了 %s 但没用到" % name)

    def check_undefined(self):
        """把所有 Name 载入跟作用域对一遍。**保守**：拿不准就不报。"""
        # 收集每个 load 是否在"它所在的那一层或更外层"有定义 —— 简化成全局一遍，
        # 只在**整份文件里都找不到**时才报（宁可漏报，不要误报）。
        all_names = set()
        for node in ast.walk(ast.parse(self.src)):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                all_names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                all_names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        all_names.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.arg):
                all_names.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                all_names.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                all_names.update(node.names)
            elif isinstance(node, ast.comprehension):
                for t in ast.walk(node.target):
                    if isinstance(t, ast.Name):
                        all_names.add(t.id)
        for name, line in self.loads:
            if name in all_names or name in self.builtin:
                continue
            self.report(line, "undefined-name", "名字 %s 在整份文件里都找不到定义" % name)


def _docstring(node):
    try:
        return ast.get_docstring(node, clean=False) or ""
    except TypeError:
        return ""


def _is_lock_with(node):
    """这个 `with` 是不是 `with LOCK:`（含 `with LOCK, ...` 那种多项目写法）。"""
    if not isinstance(node, ast.With):
        return False
    for item in node.items:
        if isinstance(item.context_expr, ast.Name) and item.context_expr.id == "LOCK":
            return True
    return False


class LockAudit(ast.NodeVisitor):
    """查「文档声明必须已持有 LOCK」的函数，有没有在**没拿锁**的地方被调用。

    这个项目是多线程 HTTP 服务器 + 一把全局 LOCK，好几个函数的 docstring 明写
    「**调用方必须已持有 LOCK**」。漏一把锁 = 竞态，是这里最难查的一类 bug
    —— 静态查一遍比等它偶发便宜得多。

    规则：一个调用算安全，当且仅当它**词法上**落在某个 `with LOCK:` 里，
    或者它所在的函数**自己也**在"需要锁"的集合里（可以传递）。
    """

    MARK = "必须已持有 LOCK"

    def __init__(self, tree):
        self.needs = {n.name for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and self.MARK in _docstring(n)}
        self.problems = []
        self.stack = []              # [(函数名, 已嵌套的 with LOCK 层数)]

    def run(self, tree):
        self.visit(tree)
        return self.problems

    def _in_lock(self):
        return any(depth > 0 for _, depth in self.stack)

    def _cur_func(self):
        return self.stack[-1][0] if self.stack else "<module>"

    def _func(self, node):
        self.stack.append((node.name, 0))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def visit_With(self, node):
        bump = 1 if _is_lock_with(node) else 0
        name, depth = self.stack[-1] if self.stack else ("<module>", 0)
        if self.stack:
            self.stack[-1] = (name, depth + bump)
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.visit(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)
        if self.stack:
            self.stack[-1] = (name, depth)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name) and f.id in self.needs:
            if not self._in_lock() and self._cur_func() not in self.needs:
                self.problems.append(
                    (node.lineno, "missing-lock",
                     "调用 %s() 时没拿 LOCK（它要求调用方已持有）—— 所在函数: %s"
                     % (f.id, self._cur_func())))
        self.generic_visit(node)


def audit_lock(path, tree):
    return LockAudit(tree).run(tree)


def main(argv):
    files = target_files(argv)
    total = 0
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError as exc:
            print("%s: 读不了（%s）" % (path, exc))
            continue
        try:
            tree = ast.parse(src, filename=path)
        except SyntaxError as exc:
            print("%s:%s: 语法错误 %s" % (path, exc.lineno, exc.msg))
            total += 1
            continue
        ck = Checker(path, src)
        ck.visit(tree)
        ck.check_undefined()
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        for line, kind, msg in sorted(ck.problems):
            print("%s:%d: [%s] %s" % (rel, line, kind, msg))
            total += 1
        for line, kind, msg in sorted(audit_lock(path, tree)):
            print("%s:%d: [%s] %s" % (rel, line, kind, msg))
            total += 1
    print("\n共 %d 个文件，%d 个问题" % (len(files), total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
