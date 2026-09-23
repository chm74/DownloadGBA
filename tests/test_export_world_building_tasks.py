import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "export_world_building_tasks.py"
    spec = importlib.util.spec_from_file_location("export_world_building_tasks", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ExportWorldBuildingTasksTests(unittest.TestCase):
    def test_validate_task_csv_preserves_quoted_values(self):
        module = load_module()

        tasks = module.validate_task_csv(
            'continent,country,city,process_name_prefix\n'
            'Africa,Algeria,Algeria,Algeria\n'
            'Europe,"Example, Republic","Example, Republic",Example\n'
        )

        self.assertEqual(
            tasks,
            [
                ("Africa", "Algeria", "Algeria", "Algeria"),
                ("Europe", "Example, Republic", "Example, Republic", "Example"),
            ],
        )

    def test_validate_task_csv_rejects_duplicates(self):
        module = load_module()

        with self.assertRaisesRegex(RuntimeError, "multiple processing prefixes"):
            module.validate_task_csv(
                "continent,country,city,process_name_prefix\n"
                "Africa,Algeria,Algeria,Algeria\n"
                "Africa,Algeria,Algeria,Other\n"
            )

    def test_fetch_tasks_does_not_put_password_in_command(self):
        module = load_module()
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "continent,country,city,process_name_prefix\n"
                "Africa,Algeria,Algeria,Algeria\n"
            ),
            stderr="",
        )

        with patch.object(module.subprocess, "run", return_value=completed) as run:
            tasks = module.fetch_tasks(
                "psql",
                dbname="building",
                user="postgres",
                host="127.0.0.1",
                port=5432,
                password="secret",
            )

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("secret", command)
        self.assertEqual(environment["PGPASSWORD"], "secret")
        self.assertEqual(tasks, [("Africa", "Algeria", "Algeria", "Algeria")])

    def test_write_tasks_adds_download_directory_column(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "nested" / "tasks.csv"

            module.write_tasks(
                output_path,
                [
                    ("Africa", "Algeria", "Algeria", "Algeria"),
                    ("Africa", "Angola", "Angola", "Angola"),
                ],
            )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "continent,country,city,download_dir,process_name_prefix\n"
                "Africa,Algeria,Algeria,data\\Africa\\Algeria\\Algeria,Algeria\n"
                "Africa,Angola,Angola,data\\Africa\\Angola\\Angola,Angola\n",
            )

    def test_write_tasks_quotes_download_directory_when_names_contain_commas(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "tasks.csv"

            module.write_tasks(
                output_path,
                [("Europe", "Example, Republic", "Example, Republic", "Example")],
            )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "continent,country,city,download_dir,process_name_prefix\n"
                'Europe,"Example, Republic","Example, Republic",'
                '"data\\Europe\\Example, Republic\\Example, Republic",Example\n',
            )


if __name__ == "__main__":
    unittest.main()
