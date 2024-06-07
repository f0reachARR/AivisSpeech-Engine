"""音声合成モデル管理機能を提供する API Router"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, UploadFile

from voicevox_engine.aivm_manager import AivmManager
from voicevox_engine.model import AivmInfo

from ..dependencies import check_disabled_mutable_api


def generate_aivm_models_router(aivm_manager: AivmManager) -> APIRouter:
    """音声合成モデル管理 API Router を生成する"""

    router = APIRouter(
        prefix="/aivm_models",
        tags=["音声合成モデル管理"],
    )

    @router.get(
        "",
        response_description="インストールした音声合成モデルの情報",
    )
    def get_installed_aivm_infos() -> dict[str, AivmInfo]:
        """
        インストールした音声合成モデルの情報を返します。
        """

        return aivm_manager.get_installed_aivm_infos()

    @router.post(
        "/install",
        status_code=204,
        dependencies=[Depends(check_disabled_mutable_api)],
    )
    def install_aivm(
        file: Annotated[
            UploadFile, File(description="音声合成モデルパッケージファイル (`.aivm`)")
        ]
    ) -> None:
        """
        音声合成モデルをインストールします。
        """

        aivm_manager.install_aivm(file.file)

    @router.get(
        "/{aivm_uuid}",
    )
    def get_aivm_manifest(
        aivm_uuid: Annotated[str, Path(description="AIVM ファイルの UUID")]
    ) -> AivmInfo:
        """
        指定された音声合成モデルの情報を取得します。
        """

        return aivm_manager.get_aivm_info(aivm_uuid)

    @router.delete(
        "/{aivm_uuid}/uninstall",
        status_code=204,
        dependencies=[Depends(check_disabled_mutable_api)],
    )
    def uninstall_aivm(
        aivm_uuid: Annotated[str, Path(description="AIVM ファイルの UUID")]
    ) -> None:
        """
        指定された音声合成モデルをアンインストールします。
        """

        aivm_manager.uninstall_aivm(aivm_uuid)

    return router
