"""
/singer_info API のテスト
"""

from fastapi.testclient import TestClient


def test_get_singer_info_200(client: TestClient) -> None:
    response = client.get(
        "/singer_info", params={"speaker_uuid": "b1a81618-b27b-40d2-b0ea-27a9ad408c4b"}
    )
    # AivisSpeech Engine では未実装 (501 Not Implemented を返す)
    assert response.status_code == 501
    return
    assert response.status_code == 200