# Copyright 2013,2014 Christoph Reiter
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

import os
import gc
import re
import shelve
from xml.dom import minidom

from . import util


# Enable caching during building multiples modules if PGIDOCGEN_CACHE is set
# Not enabled by default since it would need versioning and
# only caches by gir name and not the source path...
SHELVE_CACHE = os.environ.get("PGIDOCGEN_CACHE", None)


def get_namespace(namespace, version, _cache={}):
    key = str(namespace + "." + version)
    if key in _cache:
        return _cache[key]
    if SHELVE_CACHE:
        if os.path.getsize(SHELVE_CACHE) == 0:
            # created by tempfile, replace here
            os.remove(SHELVE_CACHE)

        d = shelve.open(SHELVE_CACHE, protocol=2)
        if key in d:
            res = d[key]
            d.close()
            return res

    ns = Namespace(namespace, version)

    if SHELVE_CACHE:
        d[key] = ns
        d.close()

    _cache[key] = ns
    return ns


def _get_dom(path, _cache={}):
    # caches the last dom
    if path in _cache:
        return _cache[path]
    _cache.clear()
    # reduce peak memory
    gc.collect()
    _cache[path] = minidom.parse(path)
    return _cache[path]


class Namespace(object):

    def __init__(self, namespace, version):
        self.namespace = namespace
        self.version = version

        print "Parsing GIR (%s-%s)" % (namespace, version)
        dom = _get_dom(self.path)

        self._types = _parse_types(dom, namespace)
        self._types.update(get_cairo_types())

        # dependencies
        deps = []
        for include in dom.getElementsByTagName("include"):
            name = include.getAttribute("name")
            version = include.getAttribute("version")
            deps.append((name, version))

        # these are not always included, but we need them
        # for base types
        if namespace != "GLib":
            if ("GLib", "2.0") not in deps:
                deps.append(("GLib", "2.0"))
            if ("GObject", "2.0") not in deps and namespace != "GObject":
                deps.append(("GObject", "2.0"))

        self._dependencies = deps

    def import_module(self):
        """Imports the module and initializes all dependencies.

        Can raise ImportError.
        """

        import gi

        # This is all needed because some modules depending on GStreamer
        # segfaults if Gst.init() isn't called before introspecting them
        to_load = list(reversed(self.get_all_dependencies()))
        to_load += [(self.namespace, self.version)]

        for (namespace, version) in to_load:
            try:
                gi.require_version(namespace, version)
            except ValueError as e:
                raise ImportError(e, version)

            module = util.import_namespace(namespace)

            # this needs to be synced with module._import_dependency
            if namespace in ("Clutter", "ClutterGst", "Gst", "Grl"):
                module.init([])
            elif namespace in ("Gsf", "IBus"):
                module.init()

        return module

    def parse_private(self):
        return _parse_private(_get_dom(self.path), self.namespace)

    def parse_docs(self):
        return _parse_docs(_get_dom(self.path))

    def get_types(self):
        return self._types

    @property
    def path(self):
        key = "%s-%s" % (self.namespace, self.version)
        return util.get_gir_files()[key]

    def get_dependencies(self):
        return list(self._dependencies)

    def get_all_dependencies(self):
        loaded = []
        to_load = self.get_dependencies()
        while to_load:
            key = to_load.pop()
            if key in loaded:
                continue
            sub_ns = get_namespace(*key)
            loaded.append(key)
            to_load.extend(sub_ns.get_dependencies())

        return list(set(loaded))

    def __repr__(self):
        return "%s(%s, %s)" % (
            type(self).__name__, self.namespace, self.version)


def get_cairo_types():
    """Creates an (incomplete) c symbol to python key mapping for pycairo"""

    import cairo

    def get_mapping(obj, prefix):
        map_ = {}
        for arg in dir(obj):
            if arg.startswith("_"):
                continue
            c_name = "_".join(filter(None, ["cairo", prefix, arg]))
            map_[c_name] = obj.__module__ + "." + obj.__name__ + "." + arg
            # import ctypes
            # lib = ctypes.CDLL("libcairo.so")
            # assert getattr(lib, c_name)
        return map_

    types = {}
    types.update(get_mapping(cairo.Context, ""))
    types.update(get_mapping(cairo.Surface, "surface"))
    types.update(get_mapping(cairo.Pattern, "pattern"))
    types.update(get_mapping(cairo.Matrix, "matrix"))
    types.update(get_mapping(cairo.FontFace, "font_face"))

    return types



def _parse_types(dom, namespace):
    """Create a mapping of various C names to python names"""

    types = {}

    def add(c_name, py_name):
        if c_name in types:
            old_count = types[c_name].count(".")
            new_count = py_name.count(".")
            # prefer static methods over functions
            if old_count > new_count:
                return

        assert py_name.count("."), py_name

        # escape each potential attribute
        py_name = ".".join(
            map(util.escape_identifier,  py_name.split(".")))
        types[c_name] = py_name

    # {key of the to be replaces function: c def of the replacement}
    shadowed = {}

    # gtk_main -> Gtk.main
    # gtk_dialog_get_response_for_widget ->
    #     Gtk.Dialog.get_response_for_widget
    elements = dom.getElementsByTagName("function")
    elements += dom.getElementsByTagName("constructor")
    elements += dom.getElementsByTagName("method")
    for t in elements:
        shadows = t.getAttribute("shadows")
        local_name = t.getAttribute("name")
        c_name = t.getAttribute("c:identifier")
        assert c_name

        # Copy escaping from gi: Foo.break -> Foo.break_
        full_name = local_name
        parent = t.parentNode

        # glib:boxed toplevel in Farstream-0.1
        if not parent.getAttribute("name"):
            continue

        while parent.getAttribute("name"):
            full_name = parent.getAttribute("name") + "." + full_name
            parent = parent.parentNode

        if shadows:
            shadowed_name = ".".join(full_name.split(".")[:-1] + [shadows])
            shadowed_name = ".".join(
                map(util.escape_identifier, shadowed_name.split(".")))
            shadowed[shadowed_name] = c_name

        add(c_name, full_name)

    # enums etc. GTK_SOME_FLAG_FOO -> Gtk.SomeFlag.FOO
    for t in dom.getElementsByTagName("member"):
        c_name = t.getAttribute("c:identifier")
        assert c_name
        # only match constants
        if c_name != c_name.upper() or "_" not in c_name:
            continue
        class_name = t.parentNode.getAttribute("name")
        field_name = t.getAttribute("name").upper()
        local_name = namespace + "." + class_name + "." + field_name
        add(c_name, local_name)

    # classes
    elements = dom.getElementsByTagName("class")
    elements += dom.getElementsByTagName("interface")
    elements += dom.getElementsByTagName("enumeration")
    elements += dom.getElementsByTagName("bitfield")
    elements += dom.getElementsByTagName("callback")
    elements += dom.getElementsByTagName("union")
    elements += dom.getElementsByTagName("alias")
    for t in elements:
        # only top level
        if t.parentNode.tagName != "namespace":
            continue

        c_name = t.getAttribute("c:type")
        c_name = c_name or t.getAttribute("glib:type-name")

        # e.g. GObject _Value__data__union
        if not c_name:
            continue

        type_name = t.getAttribute("name")
        add(c_name, namespace + "." + type_name)

    # cairo_t -> cairo.Context
    for t in dom.getElementsByTagName("record"):
        if t.getAttribute("glib:is-gtype-struct-for"):
            continue
        c_name = t.getAttribute("c:type")
        # Gee-0.8 HazardPointer
        if not c_name:
            continue
        type_name = t.getAttribute("name")
        if type_name.startswith("_"):
            continue
        add(c_name, namespace + "." + type_name)

    # G_TIME_SPAN_MINUTE -> GLib.TIME_SPAN_MINUTE
    for t in dom.getElementsByTagName("constant"):
        c_name = t.getAttribute("c:type")
        if t.parentNode.tagName == "namespace":
            name = namespace + "." + t.getAttribute("name")
            add(c_name, name)

    # the keys we want to replace have should exist
    assert not (set(shadowed.keys()) - set(types.values()))

    # make c defs which are replaced point to the key of the replacement
    # so that: "gdk_threads_add_timeout_full" -> Gdk.threads_add_timeout
    for c_name, name in types.items():
        if name in shadowed:
            replacement = shadowed[name]
            types[replacement] = name

    if namespace == "GObject":
        # these come from overrides and aren't in the gir
        # e.g. G_TYPE_INT -> GObject.TYPE_INT
        from gi.repository import GObject

        for key in dir(GObject):
            if key.startswith("TYPE_"):
                types["G_" + key] = "GObject." + key
            elif key.startswith(("G_MAX", "G_MIN")):
                types[key] = "GObject." + key

        types["GBoxed"] = "GObject.GBoxed"
    elif namespace == "GLib":
        from gi.repository import GLib

        for k in dir(GLib):
            if re.match("MINU?INT\d+", k) or re.match("MAXU?INT\d+", k):
                types["G_" + k] = "GLib." + k

    return types


def _parse_private(dom, namespace):
    private = set()

    # if disguised and no record content... not perfect, but
    # we have no other way
    for record in dom.getElementsByTagName("record"):
        disguised = bool(int(record.getAttribute("disguised") or "0"))
        if disguised:
            children = record.childNodes
            if len(children) == 1 and \
                    children[0].nodeType == children[0].TEXT_NODE:
                name = namespace + "." + record.getAttribute("name")
                private.add(name)

    return private


def _parse_docs(dom):
    """Parse docs"""

    all_ = {}
    all_shadowed = {}
    parameters = {}
    sparas = {}
    returns = {}
    sreturns = {}
    signals = {}
    properties = {}
    fields = {}

    all_docs = {
        "all": all_,
        "all_shadowed": all_shadowed,
        "parameters": parameters,
        "signal-parameters": sparas,
        "returns": returns,
        "signal-returns": sreturns,
        "signals": signals,
        "properties": properties,
        "fields": fields,
    }

    tag_names = [
        [("glib:signal",), signals],
        [("field",), fields],
        [("property",), properties],
        [("parameter", "glib:signal"), sparas],
        [("parameter", "function"), parameters],
        [("parameter", "method"), parameters],
        [("parameter", "callback"), parameters],
        [("parameter", "constructor"), parameters],
        [("instance-parameter", "method"), parameters],
        [("return-value", "callback"), returns],
        [("return-value", "method"), returns],
        [("return-value", "function"), returns],
        [("return-value", "constructor"), returns],
        [("return-value", "glib:signal"), sreturns],
        [("interface",), all_],
        [("method",), all_],
        [("function",), all_],
        [("constant",), all_],
        [("record",), all_],
        [("enumeration",), all_],
        [("member",), all_],
        [("callback",), all_],
        [("alias",), all_],
        [("constructor",), all_],
        [("class",), all_],
        [("bitfield",), all_],
        # vfuncs last, since they replace normal onces in case of name clashes
        [("virtual-method",), all_],
        [("parameter", "virtual-method"), parameters],
        [("instance-parameter", "virtual-method"), parameters],
        [("return-value", "virtual-method"), returns],
    ]

    def get_child_by_tag(node, tag_name):
        for sub in node.childNodes:
            try:
                if sub.tagName == tag_name:
                    return sub
            except AttributeError:
                continue

    path_seen = set()
    path_done = set()

    for target, result in tag_names:
        tag = target[0]
        needed = target[1:]

        for e in dom.getElementsByTagName(tag):
            doc_elm = get_child_by_tag(e, "doc")
            docs = (doc_elm and doc_elm.firstChild.nodeValue) or ""
            version = e.getAttribute("version")

            # old gir had the deprecation text in the attribute, new
            # gir in the <doc-deprecated> tag
            deprecated = e.getAttribute("deprecated")
            if deprecated in "01":
                deprecated = ""

            dep_elm = get_child_by_tag(e, "doc-deprecated")
            dep_elm_string = (dep_elm and dep_elm.firstChild.nodeValue) or ""
            deprecated = dep_elm_string or deprecated

            deprecated_version = e.getAttribute("deprecated-version")

            def get_name(elm):
                # if this entry shadows another one use its name
                shadows = elm.getAttribute("shadows")
                if shadows:
                    n = shadows
                else:
                    n = elm.getAttribute("name") or \
                        elm.getAttribute("glib:name")
                if elm.tagName == "virtual-method":
                    # pgi/pygobject escape before prefixing
                    n = "do_" + util.escape_identifier(n)
                return n

            l = []
            tags = []
            current = e
            l.append(get_name(current))
            shadowed = False
            while current.tagName != "namespace":
                # this gets shadowed by another entry, bail out
                if current.getAttribute("shadowed-by"):
                    shadowed = True
                tags.append(current.tagName)
                current = current.parentNode
                # Tracker-0.16 includes <constant> outside of <namespace>
                if current.tagName == "repository":
                    break
                name = get_name(current)
                l.insert(0, name)

            # for shadowed function docs we save docs anyway since we
            # can include them in the function docs for the replacement.
            # This can be helpful since some replacements just reference
            # the shadowed function, which we don't include.
            if result is all_shadowed:
                result = all_
            if shadowed:
                if result is all_:
                    result = all_shadowed
                else:
                    continue

            path_seen.add(tuple(tags))

            if any(a for a in needed if a not in tags):
                continue

            path_done.add(tuple(tags))

            # special case: GLib.IConv._
            if tag != "return-value" and not l[-1]:
                l[-1] = "_"
            l = filter(None, l)
            key = ".".join(map(util.escape_identifier, l))

            if tag in ("method", "constructor"):
                assert len(l) > 2

            new = (docs, version, deprecated_version, deprecated)
            # Atspi-2.0 has some things declared twice, so
            # don't be too strict here.

            # We prefix vfuncs with "do_", but this could still clash here
            if "virtual-method" not in target:
                assert key not in result or new == result[key], key
            result[key] = new

    assert not (path_seen - path_done)

    return all_docs
