from app.core.config import settings
from app.services.policy_data_service import PolicyDataService


def test_get_service_key_accepts_data_go_kr_encoded_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_go_kr_service_key", "alpha%2Bbeta%2Fgamma%3D")

    assert PolicyDataService._get_service_key() == "alpha+beta/gamma="


def test_get_service_key_preserves_data_go_kr_decoded_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_go_kr_service_key", "alpha+beta/gamma=")

    assert PolicyDataService._get_service_key() == "alpha+beta/gamma="
