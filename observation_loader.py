import json
from pathlib import Path


class ObservationLoader:

    @staticmethod
    def load_exchange(exchange: str):

        filename = Path(f"audit_metrics_{exchange}.json")

        if not filename.exists():
            raise FileNotFoundError(
                f"{filename} introuvable"
            )

        with filename.open("r", encoding="utf-8") as f:
            data = json.load(f)

        return data["pairs"]
