#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_config_xml.py
# Description: Structural checks on the plugin's dialog XML. Exists because
#              v5.75.0 added a second WEB DASHBOARD section that reused three
#              field IDs from the first, and Indigo refuses to open a dialog
#              with a duplicate ID —
#                runConfigDialog() caught exception: PAXDialogControllerError
#                -- Field ID separator_dashboard was already used.
#              Nothing in the plugin exercises the XML, so the whole Configure
#              dialog was dead for five days before anyone opened it. These
#              tests are cheap and would have caught it the same afternoon.
# Author:      CliveS & Claude Opus 5
# Date:        29-08-2026
# Version:     1.0

import collections
import glob
import os
import unittest
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
XML_FILES = sorted(glob.glob(os.path.join(HERE, "*.xml")))


def _dialog_scopes(root):
    """Every element whose direct <Field> children share one dialog namespace.

    PluginConfig.xml is itself one dialog; Devices/Actions/Events wrap each
    dialog in its own <ConfigUI>, so IDs only need to be unique within one.
    """
    scopes = [("<root>", root)]
    for parent in root.iter():
        for cfg in parent.findall("ConfigUI"):
            scopes.append((parent.get("id") or parent.tag, cfg))
    return scopes


class TestDialogXml(unittest.TestCase):

    def test_there_are_xml_files_to_check(self):
        # A glob that silently matches nothing would make every test below pass.
        self.assertTrue(XML_FILES, "no XML files found — the check is not checking")

    def test_every_xml_file_parses(self):
        for path in XML_FILES:
            with self.subTest(xml=os.path.basename(path)):
                ET.parse(path)

    def test_field_ids_unique_within_each_dialog(self):
        for path in XML_FILES:
            root = ET.parse(path).getroot()
            for label, scope in _dialog_scopes(root):
                ids = [f.get("id") for f in scope.findall("Field")]
                dupes = sorted(i for i, c in collections.Counter(ids).items() if c > 1)
                with self.subTest(xml=os.path.basename(path), dialog=label):
                    self.assertEqual(
                        dupes, [],
                        f"duplicate Field id(s) {dupes} — Indigo raises "
                        f"PAXDialogControllerError and the dialog will not open")

    def test_every_field_has_an_id(self):
        for path in XML_FILES:
            root = ET.parse(path).getroot()
            for label, scope in _dialog_scopes(root):
                for f in scope.findall("Field"):
                    with self.subTest(xml=os.path.basename(path), dialog=label):
                        self.assertTrue(f.get("id"), "a <Field> has no id attribute")

    def test_visible_bindings_point_at_a_field_in_the_same_dialog(self):
        """A visibleBindingId naming a field that isn't there hides the row for good."""
        for path in XML_FILES:
            root = ET.parse(path).getroot()
            for label, scope in _dialog_scopes(root):
                ids = {f.get("id") for f in scope.findall("Field")}
                for f in scope.findall("Field"):
                    binding = f.get("visibleBindingId")
                    if binding:
                        with self.subTest(xml=os.path.basename(path),
                                          dialog=label, field=f.get("id")):
                            self.assertIn(
                                binding, ids,
                                f"visibleBindingId {binding!r} names no field in this dialog")


class TestCurrentModeTokensMatchDevicesXml(unittest.TestCase):
    """Every ACTION_MODE_TOKEN value must exist as an <Option value=> on the
    currentMode List state.

    Indigo derives one BoolTrueFalse sub-state per Option, so a token with no
    Option writes a value the enum does not know: no sub-state fires and a
    trigger built on it never runs. Caught live 03-Sep-2026 — v5.81.0 added
    ACTION_SAVING_SESSION -> "savingSession" and did NOT add the Option, so
    `currentMode.savingSession` did not exist on the device after a restart.
    """

    def test_every_token_has_an_option(self):
        import re
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "Devices.xml"), encoding="utf-8") as fh:
            xml = fh.read()
        block = re.search(r'<State id="currentMode">.*?</ValueType>', xml, re.S)
        self.assertIsNotNone(block, "currentMode List state not found in Devices.xml")
        options = set(re.findall(r'<Option value="([^"]+)"', block.group(0)))
        self.assertTrue(options, "no Options parsed - the regex matched nothing")

        with open(os.path.join(here, "plugin.py"), encoding="utf-8") as fh:
            plugin_src = fh.read()
        table = re.search(r'ACTION_MODE_TOKEN\s*=\s*\{(.*?)\}', plugin_src, re.S)
        self.assertIsNotNone(table, "ACTION_MODE_TOKEN table not found in plugin.py")
        tokens = set(re.findall(r':\s*"([A-Za-z]+)"', table.group(1)))
        self.assertTrue(tokens, "no tokens parsed - the regex matched nothing")

        missing = sorted(tokens - options)
        self.assertEqual(missing, [],
                         f"currentMode tokens with no <Option> in Devices.xml: {missing}")



# Indigo right-aligns every control's <Label> and places the control immediately to its
# right (official-plugin-xml.md: "each of those Label elements is right aligned and the
# actual control is left aligned directly to the right of the label"). So the WIDEST
# label sets the control column for the entire dialog, and the dialog window has a hard
# maximum width — MEASURED 07-09-2026 on this Mac: System Events refused to set the
# Configure window wider than 1041 pt, from both 1200 and 2000.
#
# With a 231-character label present, all six pop-up buttons sat at window-local x=1425
# (measured via System Events, all six identical) — 384 pt beyond the right edge of a
# 1041-wide window. Every field, checkbox and menu in the dialog was off-screen, with no
# horizontal scrollbar and no way to widen it. The dialog opened and could not be used.
#
# 100 characters is a deliberately loose cap: the longest label that survives today is 82
# (siteLocationName / siteArraysJson), and 231 is what broke it. Anything approaching the
# cap belongs in a type="label" field, which is what those are for — the docs call them a
# way "to communicate a much longer chunk of text - like instructions".
MAX_CONTROL_LABEL_CHARS = 100


class TestControlLabelsStayShort(unittest.TestCase):
    """A paragraph used as a control's Label pushes every control off the dialog."""

    def _control_labels(self):
        found = []
        for path in XML_FILES:
            root = ET.parse(path).getroot()
            for field in root.iter("Field"):
                if field.get("type") in ("label", "separator"):
                    continue
                label = field.find("Label")
                text = (label.text or "").strip() if label is not None else ""
                found.append((os.path.basename(path), field.get("id"), text))
        return found

    def test_there_are_control_labels_to_check(self):
        """A scan that matches nothing passes every assertion after it."""
        self.assertGreater(len(self._control_labels()), 20)

    def test_no_control_label_is_a_paragraph(self):
        too_long = [
            (f, fid, len(t))
            for f, fid, t in self._control_labels()
            if len(t) > MAX_CONTROL_LABEL_CHARS
        ]
        self.assertEqual(
            too_long, [],
            "These control Labels are long enough to push the control column off the "
            "dialog — move the prose into a type=\"label\" field below the control:\n"
            + "\n".join(f"  {f} {fid}: {n} chars" for f, fid, n in too_long))

if __name__ == "__main__":
    unittest.main(verbosity=2)
