"""Normalisation and validation of dfnWorks job specifications.

The Windows side never builds a dfnWorks input file directly: it validates a
small, explicit parameter set here so a typo is rejected before WSL starts.
"""

from __future__ import annotations

import copy
import math
from typing import Any

STAGES = ("generate", "mesh", "graph")
SHAPES = {"rect", "ell", "ellipse"}
DISTRIBUTIONS = {"tpl", "log_normal", "exp", "constant"}
BOUNDARIES = {"left", "right", "front", "back", "top", "bottom"}
GRAPH_TYPES = {"fracture", "intersection", "bipartite"}

# Keyword arguments accepted by DFNWORKS.add_fracture_family.
FAMILY_KEYS = {
    "shape",
    "distribution",
    "orientation_distribution",
    "kappa",
    "kappa1",
    "kappa2",
    "family_number",
    "probability",
    "p32",
    "layer",
    "region",
    "number_of_points",
    "aspect",
    "beta_distribution",
    "beta",
    "theta",
    "phi",
    "strike",
    "dip",
    "trend",
    "plunge",
    "alpha",
    "log_mean",
    "log_std",
    "exp_mean",
    "constant",
    "min_radius",
    "max_radius",
    "hy_variable",
    "hy_function",
    "hy_params",
}

DEFAULTS: dict[str, Any] = {
    "domain": [100.0, 100.0, 100.0],
    "h": None,  # derived from the domain, see _default_h
    "n_poly": 100,
    "seed": 20261101,
    "orientation_option": 1,
    "stop_condition": 0,
    "boundary_faces": [1, 1, 0, 0, 0, 0],
    "ncpu": 4,
    "output_report": False,
    "uniform_mesh": True,
    "min_dist": None,  # derived from h, see _default_mesh
    "max_dist": None,  # derived from h, see _default_mesh
    "max_resolution_factor": 10.0,
    "graph_type": "fracture",
    "inflow": "left",
    "outflow": "right",
    "k": 10,
}


class DfnSpecError(ValueError):
    """Raised when a dfnWorks job specification is inconsistent."""


def _number(value: Any, name: str, *, low: float | None = None,
            high: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DfnSpecError(f"{name} must be a number, got {value!r}") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise DfnSpecError(f"{name} must be finite, got {value!r}")
    if low is not None and result < low:
        raise DfnSpecError(f"{name} must be >= {low}, got {result}")
    if high is not None and result > high:
        raise DfnSpecError(f"{name} must be <= {high}, got {result}")
    return result


def _integer(value: Any, name: str, *, low: int | None = None,
             high: int | None = None) -> int:
    if isinstance(value, bool):
        raise DfnSpecError(f"{name} must be an integer, got a boolean")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise DfnSpecError(f"{name} must be an integer, got {value!r}") from exc
    if low is not None and result < low:
        raise DfnSpecError(f"{name} must be >= {low}, got {result}")
    if high is not None and result > high:
        raise DfnSpecError(f"{name} must be <= {high}, got {result}")
    return result


def _default_h(domain: list[float]) -> float:
    """Default LaGriT length scale: 2% of the longest domain side.

    dfnWorks discards an intersection when FRAM has to shorten it by more than
    10%, and ``checkCloseEdge`` uses the target length ``h`` as the minimum
    separation, so ``h`` must stay well below the fracture size.  With the
    default radius of 20% of the domain (``h/radius = 0.1``) a 20 m, 100 m and
    1 km domain all generate a percolating, intersecting network; coarser
    values (``h/radius >= 0.2``) end with "no intersecting fractures".
    """
    return round(max(domain) / 50.0, 6)


def _default_radius(domain: list[float]) -> float:
    """Default constant fracture radius: 20% of the longest domain side."""
    return round(max(domain) / 5.0, 6)


def _default_mesh(h: float) -> tuple[float, float]:
    """Default LaGriT near-field and far-field resolution for a given h."""
    return round(h / 4.0, 6), round(5.0 * h, 6)


def _min_edge_factor(aspect: float) -> float:
    """Minimum edge of a 4-point fracture outline divided by its radius.

    dfnWorks discretises every family with four outline points and rejects a
    family when that edge is shorter than ``3h``.  For an outline point at
    ``(aspect*r*cos t, r*sin t)`` the closest pair is
    ``min(r*sqrt(1+aspect^2), 2*aspect*r, 2*r)``.
    """
    a = max(float(aspect), 1e-9)
    return min(math.sqrt(1.0 + a * a), 2.0 * a, 2.0)


def _min_radius_for(h: float, aspect: float) -> float:
    """Smallest fracture radius (with 5% margin) that survives the 3h check."""
    return 3.0 * h / _min_edge_factor(aspect) * 1.05


def _normalise_family(
    family: dict[str, Any],
    index: int,
    *,
    domain: list[float],
    h: float,
    stop_condition: int,
    probability_share: float | None,
) -> dict[str, Any]:
    if not isinstance(family, dict):
        raise DfnSpecError(f"families[{index}] must be an object")
    unknown = sorted(set(family) - FAMILY_KEYS)
    if unknown:
        raise DfnSpecError(
            f"families[{index}] has unsupported keys: {unknown}; "
            f"allowed keys are {sorted(FAMILY_KEYS)}"
        )
    clean = {key: value for key, value in family.items() if value is not None}
    if not clean:
        raise DfnSpecError(f"families[{index}] must not be empty")

    shape = str(clean.get("shape", "rect")).lower()
    if shape not in SHAPES:
        raise DfnSpecError(
            f"families[{index}].shape must be one of {sorted(SHAPES)}"
        )
    clean["shape"] = "ell" if shape == "ellipse" else shape

    distribution = str(clean.get("distribution", "constant")).lower()
    if distribution not in DISTRIBUTIONS:
        raise DfnSpecError(
            f"families[{index}].distribution must be one of "
            f"{sorted(DISTRIBUTIONS)}"
        )
    clean["distribution"] = distribution

    scale = max(domain)
    aspect = _number(clean.get("aspect", 1.0), f"families[{index}].aspect")
    if aspect <= 0:
        raise DfnSpecError(f"families[{index}].aspect must be > 0")
    clean.setdefault("aspect", aspect)
    # dfnWorks discretises each rectangle with four outline points and stops with
    # "features too small for meshing" when the shortest such edge is below 3h.
    min_radius_needed = _min_radius_for(h, aspect)
    default_min_radius = round(min_radius_needed, 6)
    default_radius = round(max(min_radius_needed, _default_radius(domain)), 6)

    def _check_min_radius(value: Any, name: str) -> float:
        radius = _number(value, name, low=0.0)
        if radius * _min_edge_factor(aspect) < 3.0 * h:
            raise DfnSpecError(
                f"{name}={radius:g} is below dfnWorks' 3h meshing limit for "
                f"h={h:g} and aspect={aspect:g}; use >= "
                f"{3.0 * h / _min_edge_factor(aspect):.4g} or a larger h"
            )
        return radius

    if distribution == "constant":
        if "constant" not in clean:
            clean["constant"] = default_radius
        else:
            clean["constant"] = _check_min_radius(
                clean["constant"], f"families[{index}].constant"
            )
    if distribution == "tpl":
        clean.setdefault("alpha", 3.0)
        clean.setdefault("min_radius", default_min_radius)
        clean.setdefault(
            "max_radius", round(max(scale / 5.0, 2.5 * default_min_radius), 6)
        )
    if distribution == "log_normal":
        clean.setdefault("min_radius", default_min_radius)
        clean.setdefault(
            "max_radius", round(max(scale / 5.0, 5.0 * default_min_radius), 6)
        )
        clean.setdefault(
            "log_mean", round(math.log(2.0 * default_min_radius), 6)
        )
        clean.setdefault("log_std", 0.5)
    if distribution == "exp":
        clean.setdefault("min_radius", default_min_radius)
        clean.setdefault(
            "max_radius", round(max(scale / 5.0, 5.0 * default_min_radius), 6)
        )
        clean.setdefault("exp_mean", round(2.0 * default_min_radius, 6))
    if "min_radius" in clean:
        clean["min_radius"] = _check_min_radius(
            clean["min_radius"], f"families[{index}].min_radius"
        )

    for key in ("kappa", "kappa1", "kappa2", "aspect", "alpha", "log_mean",
                "log_std", "exp_mean", "constant", "min_radius", "max_radius",
                "beta", "theta", "phi", "strike", "dip", "trend", "plunge",
                "p32"):
        if key in clean:
            clean[key] = _number(clean[key], f"families[{index}].{key}")
    for key in ("family_number", "layer", "region", "number_of_points"):
        if key in clean:
            clean[key] = _integer(clean[key], f"families[{index}].{key}")
    if "aspect" in clean and clean["aspect"] <= 0:
        raise DfnSpecError(f"families[{index}].aspect must be > 0")
    if "number_of_points" in clean and clean["number_of_points"] < 3:
        raise DfnSpecError(f"families[{index}].number_of_points must be >= 3")
    if "min_radius" in clean and "max_radius" in clean:
        if clean["min_radius"] <= 0 or clean["max_radius"] <= clean["min_radius"]:
            raise DfnSpecError(
                f"families[{index}] requires 0 < min_radius < max_radius"
            )

    if stop_condition == 0:
        probability = clean.get("probability")
        if probability is None:
            if probability_share is None:
                raise DfnSpecError(
                    f"families[{index}].probability is required when "
                    "stop_condition=0"
                )
            probability = probability_share
        probability = _number(
            probability, f"families[{index}].probability", low=0.0, high=1.0
        )
        if probability <= 0:
            raise DfnSpecError(
                f"families[{index}].probability must be > 0 when stop_condition=0"
            )
        clean["probability"] = probability
    else:
        if "p32" not in clean:
            raise DfnSpecError(
                f"families[{index}].p32 is required when stop_condition=1"
            )
        clean["p32"] = _number(
            clean["p32"], f"families[{index}].p32", low=0.0
        )
        if clean["p32"] <= 0:
            raise DfnSpecError(f"families[{index}].p32 must be > 0")

    if "hy_params" in clean and not isinstance(clean["hy_params"], dict):
        raise DfnSpecError(f"families[{index}].hy_params must be an object")
    return clean


def _normalise_families(
    families: list[dict[str, Any]] | None,
    *,
    domain: list[float],
    h: float,
    stop_condition: int,
) -> list[dict[str, Any]]:
    if not families:
        # Three mutually oblique sets, mirroring dfnWorks' own "constant"
        # example: one family alone produces near-parallel planes and
        # dfnWorks stops with "no intersecting fractures".  Hydraulic
        # properties stay unset because this channel stops at meshing and
        # connectivity; they can be requested per family if needed.
        radius = round(max(_min_radius_for(h, 1.0), _default_radius(domain)), 6)
        default = {
            "shape": "rect",
            "distribution": "constant",
            "kappa": 20.0,
            "aspect": 1.0,
            "constant": radius,
        }
        families = [
            dict(default, probability=0.3, trend=0.0, plunge=0.0),
            dict(default, probability=0.3, trend=35.0, plunge=90.0),
            dict(default, probability=0.4, trend=70.0, plunge=45.0),
        ]
    share = 1.0 / len(families)
    return [
        _normalise_family(
            family,
            index,
            domain=domain,
            h=h,
            stop_condition=stop_condition,
            probability_share=share,
        )
        for index, family in enumerate(families)
    ]


def _normalise_boundary(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        raise DfnSpecError("boundary_faces must contain exactly 6 entries (0/1)")
    faces = []
    for index, item in enumerate(value):
        flag = _integer(item, f"boundary_faces[{index}]", low=0, high=1)
        faces.append(flag)
    if not any(faces):
        raise DfnSpecError("boundary_faces must enable at least one face")
    return faces


def build_spec(stage: str, **values: Any) -> dict[str, Any]:
    """Build a validated, fully-populated specification for one stage."""
    stage = str(stage).lower()
    if stage not in STAGES:
        raise DfnSpecError(f"stage must be one of {STAGES}, got {stage!r}")

    merged: dict[str, Any] = copy.deepcopy(DEFAULTS)
    for key, value in values.items():
        if value is not None:
            merged[key] = value

    domain = merged.get("domain") or DEFAULTS["domain"]
    if not isinstance(domain, (list, tuple)) or len(domain) != 3:
        raise DfnSpecError("domain must be a list of three lengths [x, y, z]")
    domain = [_number(item, "domain", low=0.0) for item in domain]
    if any(item <= 0 for item in domain):
        raise DfnSpecError("every domain length must be > 0")

    h = merged.get("h")
    h = _default_h(domain) if h is None else _number(h, "h", low=0.0)
    if h <= 0:
        raise DfnSpecError("h must be > 0")

    stop_condition = _integer(
        merged.get("stop_condition", 0), "stop_condition", low=0, high=1
    )
    spec: dict[str, Any] = {
        "stage": stage,
        "domain": domain,
        "h": h,
        "n_poly": _integer(merged.get("n_poly", 100), "n_poly", low=1),
        "seed": _integer(merged.get("seed", 0), "seed", low=0),
        "orientation_option": _integer(
            merged.get("orientation_option", 1),
            "orientation_option",
            low=0,
            high=2,
        ),
        "stop_condition": stop_condition,
        "boundary_faces": _normalise_boundary(
            merged.get("boundary_faces", DEFAULTS["boundary_faces"])
        ),
        "ncpu": _integer(merged.get("ncpu", 4), "ncpu", low=1, high=256),
        "output_report": bool(merged.get("output_report", False)),
        "families": _normalise_families(
            merged.get("families"),
            domain=domain,
            h=h,
            stop_condition=stop_condition,
        ),
    }

    if stage in ("mesh", "graph"):
        default_min_dist, default_max_dist = _default_mesh(h)
        mesh = {
            "uniform_mesh": bool(merged.get("uniform_mesh", True)),
            "min_dist": _number(
                merged.get("min_dist") or default_min_dist,
                "min_dist",
                low=0.0,
            ),
            "max_dist": _number(
                merged.get("max_dist") or default_max_dist,
                "max_dist",
                low=0.0,
            ),
            "max_resolution_factor": _number(
                merged.get("max_resolution_factor", 10.0),
                "max_resolution_factor",
                low=0.0,
            ),
        }
        if mesh["min_dist"] <= 0 or mesh["max_dist"] <= 0:
            raise DfnSpecError("min_dist and max_dist must be > 0")
        if mesh["max_dist"] < mesh["min_dist"]:
            raise DfnSpecError("max_dist must be >= min_dist")
        spec["mesh"] = mesh

    if stage == "graph":
        graph_type = str(merged.get("graph_type", "fracture")).lower()
        if graph_type not in GRAPH_TYPES:
            raise DfnSpecError(f"graph_type must be one of {sorted(GRAPH_TYPES)}")
        inflow = str(merged.get("inflow", "left")).lower()
        outflow = str(merged.get("outflow", "right")).lower()
        if inflow not in BOUNDARIES or outflow not in BOUNDARIES:
            raise DfnSpecError(f"inflow/outflow must be one of {sorted(BOUNDARIES)}")
        if inflow == outflow:
            raise DfnSpecError("inflow and outflow must differ")
        spec["graph"] = {
            "graph_type": graph_type,
            "inflow": inflow,
            "outflow": outflow,
            "k": _integer(merged.get("k", 10), "k", low=1, high=1000),
        }

    return spec


def merge_spec(
    base: dict[str, Any], stage: str, **overrides: Any
) -> dict[str, Any]:
    """Derive a mesh/graph specification from a previously generated job."""
    if not isinstance(base, dict) or not base.get("families"):
        raise DfnSpecError("base specification is empty or invalid")
    merged = copy.deepcopy(base)
    merged.pop("stage", None)
    merged.pop("mesh", None)
    merged.pop("graph", None)
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return build_spec(stage, **merged)
