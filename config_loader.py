import yaml
from pathlib import Path


class Config:
    def __init__(self, config_file="config.yml"):
        self.config_file = config_file
        self.config = self._load_config()

    def _load_config(self):
        config_path = Path(self.config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file {self.config_file} not found")
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    @property
    def host(self):
        return self.config.get("server", {}).get("host", "0.0.0.0")

    @property
    def port(self):
        return self.config.get("server", {}).get("port", 5001)

    @property
    def debug(self):
        return self.config.get("server", {}).get("debug", False)

    @property
    def passwd(self):
        return self.config.get("settings", {}).get("passwd", "wiz")

    @property
    def output_dir(self):
        return self.config.get("settings", {}).get("output_dir", "output")

    @property
    def max_concurrent(self):
        return self.config.get("settings", {}).get("max_concurrent", 1)

    @property
    def auto_refresh_interval(self):
        return self.config.get("settings", {}).get("auto_refresh_interval", 5)

    @property
    def max_queue_size(self):
        return self.config.get("settings", {}).get("max_queue_size", 100)

    def reload(self):
        self.config = self._load_config()
