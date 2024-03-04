"""
/mora_data API のテスト
"""

from test.e2e.single_api.utils import gen_mora

from fastapi.testclient import TestClient


def test_post_mora_data_200(client: TestClient) -> None:
    accent_phrases = [
        {
            "moras": [
                gen_mora("テ", "t", 2.3, "e", 0.8, 3.3),
                gen_mora("ス", "s", 2.1, "U", 0.3, 0.0),
                gen_mora("ト", "t", 2.3, "o", 1.8, 4.1),
            ],
            "accent": 1,
            "pause_mora": None,
            "is_interrogative": False,
        }
    ]
    response = client.post("/mora_data", params={"speaker": 0}, json=accent_phrases)
    assert response.status_code == 200