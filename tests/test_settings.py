"""ss_settings.ini — откат нововведений без замены exe."""
import os
import shutil
import tempfile
import unittest

import settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="st_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text):
        path = os.path.join(self.tmp, "ss_settings.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_no_file_means_new_behaviour(self):
        values, found, errors = settings.load(path=os.path.join(self.tmp, "нет.ini"))
        self.assertIsNone(found)
        self.assertEqual(errors, [])
        self.assertTrue(values[("pila", "print_queue")])
        self.assertEqual(values[("pila", "batch_max")], 5)
        self.assertTrue(values[("print", "focus_lock")])

    def test_legacy_mode_turns_everything_back_to_26_10_5(self):
        values, found, errors = settings.load(path=self.write("[general]\nmode = legacy\n"))
        self.assertEqual(errors, [])
        self.assertEqual(found, os.path.join(self.tmp, "ss_settings.ini"))
        self.assertFalse(values[("pila", "print_queue")])
        self.assertEqual(values[("pila", "batch_max")], 1)
        self.assertFalse(values[("pila", "label_text")])
        self.assertFalse(values[("pila", "fast_validate")])
        self.assertFalse(values[("pila", "mark_retry")])
        self.assertFalse(values[("print", "focus_lock")])
        self.assertEqual(values[("print", "timeout_s")], 60)

    def test_single_switch_overrides_mode(self):
        values, _, errors = settings.load(path=self.write(
            "[general]\nmode = legacy\n[print]\nfocus_lock = on ; только блокировка фокуса\n"))
        self.assertEqual(errors, [])
        self.assertTrue(values[("print", "focus_lock")])
        self.assertFalse(values[("pila", "print_queue")])

    def test_russian_and_numeric_booleans(self):
        values, _, errors = settings.load(path=self.write("[pila]\nprint_queue = выкл\nlabel_text = 0\n"))
        self.assertEqual(errors, [])
        self.assertFalse(values[("pila", "print_queue")])
        self.assertFalse(values[("pila", "label_text")])

    def test_bad_values_fall_back_to_defaults_and_are_reported(self):
        values, _, errors = settings.load(path=self.write(
            "[general]\nmode = старый\n[pila]\nbatch_max = много\nprint_queue = может\n[print]\ntimeout_s = 0\n"))
        self.assertEqual(values[("pila", "batch_max")], 5)
        self.assertTrue(values[("pila", "print_queue")])
        self.assertEqual(values[("print", "timeout_s")], 30)
        self.assertEqual(len(errors), 4)

    def write_bytes(self, data):
        path = os.path.join(self.tmp, "ss_settings.ini")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_notepad_ansi_cp1251_with_russian_comments(self):
        text = "; откат на старую версию\r\n[general]\r\nmode = legacy ; как в 26.10.5\r\n"
        values, found, errors = settings.load(path=self.write_bytes(text.encode("cp1251")))
        self.assertIsNotNone(found)
        self.assertEqual(errors, [])
        self.assertFalse(values[("pila", "print_queue")])

    def test_notepad_unicode_utf16_with_bom(self):
        text = "; откат\r\n[pila]\r\nprint_queue = off\r\n"
        values, _, errors = settings.load(path=self.write_bytes(text.encode("utf-16")))
        self.assertEqual(errors, [])
        self.assertFalse(values[("pila", "print_queue")])

    def test_utf8_with_bom(self):
        values, _, errors = settings.load(path=self.write_bytes("\ufeff[pila]\nbatch_max = 3\n".encode("utf-8")))
        self.assertEqual(errors, [])
        self.assertEqual(values[("pila", "batch_max")], 3)

    def test_percent_sign_does_not_crash(self):
        values, _, errors = settings.load(path=self.write("[pila]\nbatch_max = 5%\n; 100% откат\n"))
        self.assertEqual(values[("pila", "batch_max")], 5)
        self.assertEqual(len(errors), 1, errors)

    def test_unknown_section_and_key_are_reported(self):
        values, _, errors = settings.load(path=self.write("[Pila]\nprint_queue = off\n[pila]\nprint_que = off\n"))
        self.assertTrue(values[("pila", "print_queue")], "опечатка не должна ничего выключать")
        self.assertTrue(any("[Pila]" in e for e in errors))
        self.assertTrue(any("print_que" in e for e in errors))

    def test_broken_line_does_not_cancel_valid_legacy_mode(self):
        values, _, errors = settings.load(path=self.write(
            "[general]\nmode = legacy\nэто строка без знака равенства\n[pila]\nbatch_max = 3\nbatch_max = 4\n"))
        self.assertEqual(values[("general", "mode")], "legacy", "кривая строка не отменяет откат")
        self.assertFalse(values[("pila", "print_queue")])
        self.assertEqual(values[("pila", "batch_max")], 4, "повтор ключа — берётся последнее значение")
        self.assertEqual(len(errors), 1, errors)

    def test_garbage_file_never_crashes(self):
        values, _, errors = settings.load(path=self.write_bytes(b"\x00\x01\xff\xfe\x00[\x00garbage"))
        defaults, _, _ = settings.load(path=os.path.join(self.tmp, "нет.ini"))
        self.assertEqual(values, defaults)
        self.assertTrue(errors)

    def test_example_file_parses_without_errors_and_equals_defaults(self):
        example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "ss_settings.example.ini")
        values, _, errors = settings.load(path=example)
        defaults, _, _ = settings.load(path=os.path.join(self.tmp, "нет.ini"))
        self.assertEqual(errors, [])
        self.assertEqual(values, defaults)


if __name__ == "__main__":
    unittest.main()
