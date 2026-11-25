"""Inspect the live AT-SPI desktop without interacting with WeChat."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from bridge.accessibility import AccessibilitySource, EVENT_TYPES, Node, client_version, desktop_nodes, runtime, wechat_app
from bridge.config import Settings


def _capture(read: Callable, errors: dict[str, str], name: str, default: Any = None) -> Any:
    try:
        return read()
    except Exception as error:
        errors[name] = str(error)
        return default


def _redact(value: str | None, enabled: bool) -> str | None:
    return "[redacted]" if enabled and value else value


def inspect_node(node: Node, path: tuple[int, ...], *, redact: bool) -> dict:
    errors: dict[str, str] = {}
    interfaces = _capture(lambda: sorted(node.interfaces), errors, "interfaces", [])
    output = {
        "path": list(path),
        "role": _capture(lambda: node.role, errors, "role"),
        "name": _redact(_capture(lambda: node.name, errors, "name"), redact),
        "description": _redact(_capture(lambda: node.description, errors, "description"), redact),
        "accessible_id": _capture(lambda: node.accessible_id, errors, "accessible_id"),
        "attributes": _capture(lambda: node.attributes, errors, "attributes", {}),
        "interfaces": interfaces,
        "states": _capture(lambda: sorted(node.states), errors, "states", []),
        "bounds": _capture(lambda: node.bounds, errors, "bounds"),
        "supports": {
            key: name in interfaces for key, name in (
                ("text", "Text"), ("image", "Image"),
                ("hypertext", "Hypertext"), ("hyperlink", "Hyperlink"), ("value", "Value"),
            )
        },
    }
    if redact:
        output["accessible_id"] = _redact(output["accessible_id"], True)
        output["attributes"] = {key: "[redacted]" for key in output["attributes"]}
    if output["supports"]["text"]:
        output["text"] = _redact(_capture(lambda: node.text, errors, "text"), redact)
    if output["supports"]["image"]:
        output["image_bounds"] = _capture(lambda: node.image_bounds, errors, "image_bounds")
        output["image_description"] = _redact(_capture(
            node.native.get_image_description, errors, "image_description",
        ), redact)
    if output["supports"]["hypertext"] or output["supports"]["hyperlink"]:
        uris = _capture(lambda: node.uris, errors, "uris", ())
        output["uris"] = [_redact(uri, redact) for uri in uris]
    if output["supports"]["value"]:
        output["value"] = {
            key: _capture(lambda method=method: getattr(node.value, method)(), errors, key)
            for key, method in (
                ("current", "get_current_value"), ("minimum", "get_minimum_value"),
                ("maximum", "get_maximum_value"), ("increment", "get_minimum_increment"),
            )
        }
    if errors:
        output["errors"] = errors
    return output


def inspect_tree(root: Node, *, max_nodes: int, max_depth: int, redact: bool) -> dict:
    count = 0
    truncated = False

    def visit(node: Node, path: tuple[int, ...]) -> dict:
        nonlocal count, truncated
        count += 1
        tree = inspect_node(node, path, redact=redact and bool(path))
        errors = tree.get("errors", {})
        child_count = _capture(node.native.get_child_count, errors, "children", 0)
        tree["children"] = []
        if child_count < 0:
            errors["children"] = "negative child count"
        elif child_count and len(path) >= max_depth:
            truncated = True
        else:
            for index in range(child_count):
                if count >= max_nodes:
                    truncated = True
                    break
                child = _capture(lambda: node.child(index), errors, f"child_{index}")
                if child is not None:
                    tree["children"].append(visit(child, (*path, index)))
                else:
                    # A disappearing child still consumes the traversal budget.
                    count += 1
                    errors.setdefault(f"child_{index}", "child disappeared")
        if errors:
            tree["errors"] = errors
        return tree

    tree = visit(root, ())
    return {"tree": tree, "node_count": count, "truncated": truncated}


def collect_events(atspi, glib, seconds: float, *, redact: bool) -> dict:
    records: list[dict] = []
    registered: list[str] = []
    errors: dict[str, str] = {}
    dropped = 0

    def on_event(event, *_user_data):
        nonlocal dropped
        if len(records) >= 2000:
            dropped += 1
            return
        details: dict[str, str] = {}
        record = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "type": event.type, "detail1": event.detail1, "detail2": event.detail2,
        }
        if event.source is not None:
            source = Node(event.source, atspi)
            record["source"] = {
                "role": _capture(lambda: source.role, details, "role"),
                "name": _redact(_capture(lambda: source.name, details, "name"), redact),
                "pid": _capture(source.native.get_process_id, details, "pid"),
            }
        if details:
            record["errors"] = details
        records.append(record)

    listener = atspi.EventListener.new(on_event)
    try:
        for kind in EVENT_TYPES:
            if _capture(lambda: listener.register(kind), errors, kind, False):
                registered.append(kind)
            else:
                errors.setdefault(kind, "event registration declined")
        end = time.monotonic() + seconds
        context = glib.MainContext.default()
        while time.monotonic() < end:
            if context.pending():
                context.iteration(False)
            else:
                time.sleep(0.015)
    finally:
        for kind in registered:
            _capture(lambda: listener.deregister(kind), errors, f"deregister:{kind}")
    return {"seconds": seconds, "registered": registered, "records": records, "dropped": dropped, "errors": errors}


def _main_window_candidates(tree: dict) -> list[list[int]]:
    candidates = []
    stack = list(tree["children"])
    while stack:
        child = stack.pop()
        if child["role"] in {"frame", "window", "dialog"}:
            candidates.append(child["path"])
        stack.extend(child["children"])
    return sorted(candidates)


def probe(args: argparse.Namespace) -> dict:
    recorded_at = datetime.now(timezone.utc).isoformat()
    result = {
        "observed_at": recorded_at,
        "architecture": platform.machine(),
        "wechat_version": client_version(),
        "applications": [], "trees": [], "errors": {},
    }
    started = False
    try:
        atspi, glib = runtime()
        if atspi.init() not in (0, 1):
            raise RuntimeError("AT-SPI session initialization failed")
        started = True
        atspi.set_timeout(900, 900)
        if args.check_profile:
            try:
                settings = Settings.model_validate(yaml.safe_load(args.check_profile.read_text(encoding="utf-8")) or {})
                source = AccessibilitySource(settings.chat, profile=settings.accessibility_profile)
            except (OSError, ValueError, yaml.YAMLError) as error:
                result["errors"]["config"] = type(error).__name__
            else:
                result["profile_check"] = source.inspect_profile(atspi, result["wechat_version"])
            return result
        for application in desktop_nodes(atspi):
            errors: dict[str, str] = {}
            name = _capture(lambda: application.name, errors, "name")
            pid = _capture(application.native.get_process_id, errors, "pid")
            summary = {
                "name": name, "pid": pid,
                "role": _capture(lambda: application.role, errors, "role"),
                "toolkit": _capture(application.native.get_toolkit_name, errors, "toolkit"),
            }
            if errors:
                summary["errors"] = errors
            result["applications"].append(summary)
            selected = args.all or (name and args.app and name.casefold() == args.app.casefold())
            if selected or (not args.app and name and wechat_app(application)):
                tree_data = inspect_tree(
                    application, max_nodes=args.max_nodes, max_depth=args.max_depth, redact=args.redact,
                )
                result["trees"].append({
                    "application": name, "pid": pid,
                    "window_candidates": _main_window_candidates(tree_data["tree"]),
                    **tree_data,
                })
        if args.events:
            result["events"] = collect_events(atspi, glib, args.events, redact=args.redact)
    except (ImportError, ValueError, RuntimeError) as error:
        result["errors"]["session"] = str(error)
    finally:
        if started:
            atspi.exit()
    return result


def _print_tree(node: dict, depth: int = 0) -> None:
    details = {key: value for key, value in node.items() if key not in {"path", "children"}}
    print("  " * depth + f"{node['path']} " + json.dumps(details, ensure_ascii=False))
    for child in node["children"]:
        _print_tree(child, depth + 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="dump trees for all applications")
    parser.add_argument("--app", help="dump the tree for this exact application name")
    parser.add_argument("--check-profile", type=Path, metavar="CONFIG",
                        help="validate this Bridge config against the current window without changing the desktop")
    parser.add_argument("--events", type=float, default=0, metavar="SECONDS", help="also record live events")
    parser.add_argument("--json", dest="json_output", metavar="FILE", help="write JSON; use - for stdout")
    parser.add_argument("--max-nodes", type=int, default=10000, help="maximum nodes per application")
    parser.add_argument("--max-depth", type=int, default=80)
    parser.add_argument("--redact", action="store_true", help="hide widget names, text, descriptions and URLs")
    args = parser.parse_args()
    if not 0 <= args.events <= 3600 or args.max_nodes < 1 or not 1 <= args.max_depth <= 500:
        parser.error("expected 0–3600 event seconds, a positive node limit and depth between 1 and 500")
    if args.check_profile and (args.all or args.app or args.events):
        parser.error("profile validation is separate from tree dumps and event recording")

    report = probe(args)
    if args.json_output:
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.json_output == "-":
            print(payload, end="")
        else:
            # Diagnostics may contain personal chat data. Create files only for the current user.
            descriptor = os.open(Path(args.json_output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                destination.write(payload)
    else:
        print(f"Architecture: {report['architecture']}")
        print(f"WeChat package version: {report['wechat_version'] or 'unknown'}")
        if args.check_profile:
            if "profile_check" in report:
                print(json.dumps(report["profile_check"], ensure_ascii=False, indent=2))
        else:
            print(f"Applications: {json.dumps(report['applications'], ensure_ascii=False)}")
            if not report["trees"] and not report["errors"]:
                print("No matching AT-SPI application tree was found.")
            for tree in report["trees"]:
                print(f"{tree['application']} PID {tree['pid']} ({tree['node_count']} nodes; "
                      f"truncated={tree['truncated']}) Window candidates: {tree['window_candidates']}")
                _print_tree(tree["tree"])
        if "events" in report:
            print("Events: " + json.dumps(report["events"], ensure_ascii=False))
        if report["errors"]:
            print(json.dumps(report["errors"], ensure_ascii=False), file=sys.stderr)
    if report["errors"]:
        return 1
    if args.check_profile and report["profile_check"]["state"] != "ready":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
