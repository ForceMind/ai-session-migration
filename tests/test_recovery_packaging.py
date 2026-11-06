import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_session_migration.__main__ import synthetic_file_acceptance, main
from ai_session_migration.state import AppState


class PackagedRecoveryTest(unittest.TestCase):
    def test_synthetic_entry_exercises_actual_recovery_stack(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder).resolve();home=base/'home';home.mkdir()
            window=SimpleNamespace(home=home,state=AppState(base/'state'),records=[SimpleNamespace(session_id=x) for x in ['claude-session','codex-session','harness-session']])
            result=synthetic_file_acceptance(window)
            self.assertTrue(result['ok'])
            self.assertTrue(result['rollbackVerified'])
            self.assertFalse(result['nativeClientRestored'])

    def test_qualification_refuses_real_home_and_unknown_records(self):
        for home,records in [(Path.home(),[]),(Path('/isolated'),[SimpleNamespace(session_id='real-session')])]:
            with self.assertRaises(ValueError):synthetic_file_acceptance(SimpleNamespace(home=home,records=records))

    def test_recovery_hook_requires_explicit_acceptance_environment(self):
        with patch.dict(os.environ,{'ASM_ACCEPTANCE_TEST':''}),patch('sys.argv',['client','--smoke-recovery']):
            with self.assertRaises(SystemExit):main()
