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


if __name__ == "__main__":
    unittest.main(verbosity=2)
