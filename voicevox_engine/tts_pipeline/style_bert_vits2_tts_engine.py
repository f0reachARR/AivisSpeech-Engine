# flake8: noqa

import copy
import time
from io import BytesIO
from typing import Literal

import aivmlib
import jaconv
import numpy as np
import torch
import torch.version
from fastapi import HTTPException
from numpy.typing import NDArray
from style_bert_vits2.constants import (
    DEFAULT_SDP_RATIO,
    DEFAULT_STYLE_WEIGHT,
    Languages,
)
from style_bert_vits2.logging import logger as style_bert_vits2_logger
from style_bert_vits2.models.hyper_parameters import HyperParameters
from style_bert_vits2.nlp import bert_models
from style_bert_vits2.nlp.japanese.g2p import g2p
from style_bert_vits2.nlp.japanese.g2p_utils import g2kata_tone, kata_tone2phone_tone
from style_bert_vits2.nlp.japanese.mora_list import (
    CONSONANTS,
    MORA_PHONEMES_TO_MORA_KATA,
)
from style_bert_vits2.nlp.japanese.normalizer import normalize_text
from style_bert_vits2.nlp.symbols import PUNCTUATIONS
from style_bert_vits2.tts_model import TTSModel

from ..aivm_manager import AivmManager
from ..core.core_adapter import CoreAdapter, DeviceSupport
from ..dev.core.mock import MockCoreWrapper
from ..logging import logger
from ..metas.Metas import StyleId
from ..model import AudioQuery
from ..tts_pipeline.model import AccentPhrase, Mora
from ..tts_pipeline.tts_engine import (
    TTSEngine,
    raw_wave_to_output_wave,
    to_flatten_moras,
)
from ..utility.path_utility import get_save_dir


class StyleBertVITS2TTSEngine(TTSEngine):
    """Style-Bert-VITS2 TTS Engine"""

    # BERT モデルのキャッシュディレクトリ
    BERT_MODEL_CACHE_DIR = get_save_dir() / "bert_model_caches"

    def __init__(
        self,
        aivm_manager: AivmManager,
        use_gpu: bool = False,
        load_all_models: bool = False,
    ) -> None:
        self.aivm_manager = aivm_manager
        self.use_gpu = use_gpu
        self.load_all_models = load_all_models

        # ロード済みモデルのキャッシュ
        self.tts_models: dict[str, TTSModel] = {}

        # PyTorch での推論に利用するデバイスを選択
        self.device: Literal["cpu", "cuda", "mps"] = "cpu"
        self.is_gpu_available: bool = False
        # NVIDIA GPU が接続されているなら CUDA (Compute Unified Device Architecture) を利用できる
        if torch.backends.cuda.is_built() and torch.cuda.is_available():
            self.is_gpu_available = True
            # GPU モード指定時
            if use_gpu is True:
                self.device = "cuda"
                # AMD ROCm は PyTorch 上において CUDA デバイスとして認識される
                ## torch.version.hip が None でなければ CUDA ではなく ROCm が利用されていると判断できるらしい
                ## ref: https://pytorch.org/docs/stable/notes/hip.html
                if torch.version.hip is not None:
                    logger.info("Using GPU (AMD ROCm) for inference.")
                else:
                    logger.info("Using GPU (NVIDIA CUDA) for inference.")
            # CPU モード指定時
            else:
                if torch.version.hip is not None:
                    logger.info("Using CPU for inference. (but AMD ROCm is available!)")
                else:
                    logger.info("Using CPU for inference. (but NVIDIA CUDA is available!)")  # fmt: skip
        # Mac なら基本 Apple MPS (Metal Performance Shaders) が利用できる
        # FIXME: SDP Ratio の値次第で話速が遅くなる謎の問題がある上 CPU と推論速度が然程変わらないため、当面コメントアウト
        # elif torch.backends.mps.is_built() and torch.backends.mps.is_available():
        #     self.is_gpu_available = True
        #     # GPU モード指定時
        #     if use_gpu is True:
        #         self.device = "mps"
        #         logger.info("Using GPU (Apple MPS) for inference.")
        #     # CPU モード指定時
        #     else:
        #         logger.info("Using CPU for inference. (but Apple MPS is available!)")
        # それ以外の環境では CPU にフォールバック
        else:
            self.is_gpu_available = False
            # GPU モード指定時
            if use_gpu is True:
                self.device = "cpu"
                logger.warning("GPU is not available. Using CPU instead.")
            # CPU モード指定時
            else:
                logger.info("Using CPU for inference.")

        # Style-Bert-VITS2 本体のロガーを抑制
        style_bert_vits2_logger.remove()

        # 音声合成に必要な BERT モデル・トークナイザーを読み込む
        ## 一度ロードすればプロセス内でグローバルに保持される
        logger.info("Loading BERT model and tokenizer...")
        bert_models.load_model(
            language=Languages.JP,
            pretrained_model_name_or_path="ku-nlp/deberta-v2-large-japanese-char-wwm",
            device_map=self.device,
            cache_dir=str(self.BERT_MODEL_CACHE_DIR),
        )
        bert_models.load_tokenizer(
            language=Languages.JP,
            pretrained_model_name_or_path="ku-nlp/deberta-v2-large-japanese-char-wwm",
            cache_dir=str(self.BERT_MODEL_CACHE_DIR),
        )
        logger.info("BERT model and tokenizer loaded.")

        # load_all_models が True の場合は全ての音声合成モデルをロードしておく
        if load_all_models is True:
            logger.info("Loading all models...")
            for aivm_uuid in self.aivm_manager.get_installed_aivm_infos().keys():
                self.load_model(aivm_uuid)
            logger.info("All models loaded.")

        # VOICEVOX CORE の通常の CoreWrapper の代わりに MockCoreWrapper を利用する
        ## 継承元の TTSEngine は self._core に CoreWrapper を入れた CoreAdapter のインスタンスがないと動作しない
        self._core = CoreAdapter(MockCoreWrapper())

    @property
    def default_sampling_rate(self) -> int:
        """合成される音声波形のデフォルトサンプリングレートを取得する。"""
        # Style-Bert-VITS2 の出力サンプリング周波数 (44.1KHz) に合わせる
        return 44100

    @property
    def supported_devices(self) -> DeviceSupport | None:
        """合成時に各デバイスが利用可能か否かの一覧を取得する。"""
        return DeviceSupport(
            # CPU: 常にサポートされる
            cpu=True,
            # CUDA: GPU が利用可能な場合は True (CUDA 以外の GPU も含む)
            cuda=True if self.is_gpu_available else False,
            # DirectML: 常にサポートされない
            dml=False,
        )

    def load_model(self, aivm_uuid: str) -> TTSModel:
        """
        Style-Bert-VITS2 の音声合成モデルをロードする
        StyleBertVITS2TTSEngine の初期化時に use_gpu=True が指定されている場合、モデルは GPU にロードされる
        継承元の TTSEngine には存在しない、StyleBertVITS2TTSEngine 固有のメソッド

        Parameters
        ----------
        aivm_uuid : str
            AIVM の UUID

        Returns
        -------
        TTSModel
            ロード済みの TTSModel インスタンス (キャッシュ済みの場合はそのまま返す)
        """

        # 既に読み込まれている場合はそのまま返す
        if aivm_uuid in self.tts_models:
            return self.tts_models[aivm_uuid]

        # AIVM メタデータを読み込む
        aivm_info = self.aivm_manager.get_aivm_info(aivm_uuid)
        try:
            with open(aivm_info.file_path, mode="rb") as f:
                aivm_metadata = aivmlib.read_aivm_metadata(f)
        except aivmlib.AivmValidationError as e:
            logger.error(f"{aivm_info.file_path}: Failed to read AIVM metadata. ({e})")
            raise HTTPException(
                status_code=500,
                detail="Failed to read AIVM metadata.",
            )

        # ハイパーパラメータを読み込む
        hyper_parameters = HyperParameters.model_validate(
            aivm_metadata.hyper_parameters.model_dump()
        )

        # スタイルベクトルを読み込む
        assert aivm_metadata.style_vectors is not None
        style_vectors = np.load(BytesIO(aivm_metadata.style_vectors))

        # 音声合成モデルをロード
        tts_model = TTSModel(
            # 音声合成モデルのパスとして、AIVM ファイル (Safetensors 互換) のパスを指定
            model_path=aivm_info.file_path,
            # config_path とあるが、HyperParameters の Pydantic モデルを直接指定できる
            config_path=hyper_parameters,
            # style_vec_path とあるが、style_vectors の NDArray を直接指定できる
            style_vec_path=style_vectors,
            # 音声合成モデルのロード先の GPU デバイスを指定
            device=self.device,
        )  # fmt: skip
        tts_model.load()
        logger.info(f"{aivm_info.manifest.name} ({aivm_uuid}) loaded.")

        self.tts_models[aivm_uuid] = tts_model
        return tts_model

    def is_model_loaded(self, aivm_uuid: str) -> bool:
        """
        指定された AIVM の UUID に対応する音声合成モデルがロード済みかどうかを返す
        継承元の TTSEngine には存在しない、StyleBertVITS2TTSEngine 固有のメソッド

        Parameters
        ----------
        aivm_uuid : str
            AIVM の UUID

        Returns
        -------
        bool
            モデルがロード済みかどうか
        """

        return aivm_uuid in self.tts_models

    def create_accent_phrases(self, text: str, style_id: StyleId) -> list[AccentPhrase]:
        """
        テキストからアクセント句系列を生成する
        継承元の TTSEngine.create_accent_phrases() をオーバーライドし、Style-Bert-VITS2 に適したアクセント句系列生成処理に差し替えている

        Style-Bert-VITS2 は同じ pyopenjtalk ベースの自然言語処理でありながら VOICEVOX ENGINE とアクセント関連の実装が異なるため、
        VOICEVOX ENGINE 本来のアクセント句系列生成処理は利用せず、代わりに g2p() から取得したモーラ情報と音高のリストから
        擬似的にアクセント句系列 AccentPhrase を生成する形で実装している
        こうすることで、VOICEVOX ENGINE (pyopenjtalk) では一律削除されてしまう句読点や記号を保持した状態でアクセント句系列を生成できる
        VOICEVOX ENGINE と異なりスタイル ID に基づいてその音素長・モーラ音高を更新することは原理上不可能なため、
        音素長・モーラ音高は常にダミー値で返される

        Parameters
        ----------
        text : str
            テキスト
        style_id : StyleId
            スタイル ID

        Returns
        -------
        list[AccentPhrase]
            アクセント句系列
        """

        def phone_tone2mora_tone(
            phone_tone: list[tuple[str, int]]
        ) -> list[tuple[Mora, int]]:
            """
            phone_tone の phone 部分を VOICEVOX ENGINE の Mora 型に変換する。ただし最初と最後の ("_", 0) は無視する。
            style_bert_vits2.nlp.japanese.g2p_utils.phone_tone2kata_tone() をベースに改変したもの。

            Args:
                phone_tone: 音素と音高のリスト。

            Returns:
                カタカナと音高のリスト。
            """

            phone_tone = phone_tone[1:]  # 最初の("_", 0)を無視
            phones = [phone for phone, _ in phone_tone]
            tones = [tone for _, tone in phone_tone]
            result: list[tuple[Mora, int]] = []
            current_consonant: str | None = None
            for phone, _, tone, next_tone in zip(phones, phones[1:], tones, tones[1:]):
                # zip の関係で最後の ("_", 0) は無視されている
                # "!", "?", "…", ",", ".", "'", "-" の場合
                if phone in PUNCTUATIONS:
                    # VOICEVOX ENGINE の Mora に変換
                    mora = Mora(
                        text=phone,  # 記号をそのままテキスト (カナ) とする
                        consonant=None,
                        consonant_length=None,  # AivisSpeech Engine では常にダミー値
                        vowel="pau",  # VOICEVOX ENGINE では記号は pau として扱われる
                        vowel_length=0.0,  # AivisSpeech Engine では常にダミー値
                        pitch=0.0,  # AivisSpeech Engine では常にダミー値
                    )
                    result.append((mora, tone))
                    continue
                # n 以外の子音の場合
                if phone in CONSONANTS:
                    assert current_consonant is None, f"Unexpected {phone} after {current_consonant}"  # fmt: skip
                    assert tone == next_tone, f"Unexpected {phone} tone {tone} != {next_tone}"  # fmt: skip
                    # 現在の子音を保持しておく
                    current_consonant = phone
                # phone が母音もしくは「N」の場合
                else:
                    # 母音を取得
                    current_vowel = phone
                    # VOICEVOX ENGINE の Mora に変換
                    mora = Mora(
                        # 子音 (あれば) と母音を結合して取得した、対応するカタカナ表記のモーラを設定
                        text=MORA_PHONEMES_TO_MORA_KATA[
                            ("" if current_consonant is None else current_consonant)
                            + current_vowel
                        ],
                        consonant=current_consonant,
                        consonant_length=(
                            None if current_consonant is None else 0.0
                        ),  # AivisSpeech Engine では常にダミー値
                        vowel=current_vowel,
                        vowel_length=0.0,  # AivisSpeech Engine では常にダミー値
                        pitch=0.0,  # AivisSpeech Engine では常にダミー値
                    )
                    result.append((mora, tone))
                    current_consonant = None

            return result

        # 入力テキストを Style-Bert-VITS2 の基準で正規化
        ## Style-Bert-VITS2 では「〜」などの伸ばす棒も長音記号として扱うため、normalize_text() でそれらを統一する
        normalized_text = normalize_text(text)

        # g2p 処理を行い、テキストからモーラ情報と音高 (0 or 1) のリストを取得
        ## Style-Bert-VITS2 側では、pyopenjtalk_g2p_prosody() から取得したアクセント情報が含まれるモーラのリストを
        ## モーラ情報と音高のリストに変換し (句読点や記号は失われている) 、後付けで失われた句読点や記号のモーラを適切な位置に追加する形で実装されている
        ## VOICEVOX ENGINE 側のアクセント句系列生成処理は微妙に互換性がないため使っていない
        ## VOICEVOX ENGINE では「ん」の音素を「N」としているので、use_jp_extra (「ん」の音素を「n」」とする) は常に False に設定している
        phones, tones, _, sep_kata_with_joshi = g2p(normalized_text, use_jp_extra=False, raise_yomi_error=False)  # fmt: skip
        mora_tone_list = phone_tone2mora_tone(list(zip(phones, tones)))

        # mora_tone_list を、まず記号から通常のモーラに変わったタイミングで区切ってグループ化
        # 通常のモーラから記号に変わったタイミングでは区切らない
        ## 例: 「...私は,,そう思うよ...?どうかな.」 -> [["..."], ["私", "は", ",", ","], ["そう", "思う", "よ", "...", "?"], ["どう", "か", "な", "."]]
        mora_tone_groups: list[list[tuple[Mora, int]]] = []
        current_group: list[tuple[Mora, int]] = []
        for index, (mora, tone) in enumerate(mora_tone_list):
            if index == 0 or (
                mora.text not in PUNCTUATIONS
                and mora_tone_list[index - 1][0].text in PUNCTUATIONS
            ):
                if current_group:
                    mora_tone_groups.append(current_group)
                    current_group = []
                current_group.append((mora, tone))
            else:
                current_group.append((mora, tone))
        if current_group:  # 最後のグループがあれば追加
            mora_tone_groups.append(current_group)

        # さらにグループごとに、アクセントが変わるタイミングで区切って再グループ化
        # 音高が 前: 1, 現在: 0, 次: 1 の場合、前と現在の間で区切る
        # 音高が 前: 0, 現在: 0, 次: 1 の場合、前と現在の間で区切る
        re_grouped_mora_tone_groups: list[list[list[tuple[Mora, int]]]] = []
        for group in mora_tone_groups:
            re_grouped_group: list[list[tuple[Mora, int]]] = []
            current_re_group: list[tuple[Mora, int]] = []
            for index, (mora, tone) in enumerate(group):
                # 前の音高を取得 (最初のモーラの場合は None)
                previous_tone = group[index - 1][1] if index > 0 else None
                # 後続の音高を取得 (最後のモーラの場合は None)
                next_tone = group[index + 1][1] if index < len(group) - 1 else None
                # 音高が 前: 1, 現在: 0, 次: 1 の場合、前と現在の間で区切る
                if (previous_tone == 1) and (tone == 0) and (next_tone == 1):
                    re_grouped_group.append(current_re_group)
                    current_re_group = [(mora, tone)]
                # 音高が 前: 0, 現在: 0, 次: 1 の場合、前と現在の間で区切る
                elif (previous_tone == 0) and (tone == 0) and (next_tone == 1):
                    re_grouped_group.append(current_re_group)
                    current_re_group = [(mora, tone)]
                # それ以外の場合はグループに追加
                else:
                    current_re_group.append((mora, tone))
            if current_re_group:  # 最後のグループがあれば追加
                re_grouped_group.append(current_re_group)
            re_grouped_mora_tone_groups.append(re_grouped_group)

        # グループのグループのようにネスト構造になっているので、flatten する
        mora_tone_groups = [
            item for sublist in re_grouped_mora_tone_groups for item in sublist
        ]

        # mora_tone_groups を AccentPhrase に変換
        accent_phrases: list[AccentPhrase] = []
        for group in mora_tone_groups:

            # グループ内で次のモーラで音高が 1 から 0 に下がるモーラをアクセント核のインデックスとする
            # 1-indexed なので 1 から始まるインデックスに変換
            accent_index = None
            for index, (mora, tone) in enumerate(group):
                if tone == 1 and index < len(group) - 1 and group[index + 1][1] == 0:
                    accent_index = index + 1
                    break
            ## この時点でアクセント核が見つからなかった場合は最後のモーラまで音高が高いままだと思われるので、
            ## 最後のモーラをアクセント核とする
            if accent_index is None:
                accent_index = len(group)

            # グループ内のモーラをリストに変換
            moras: list[Mora] = []
            for mora, _ in group:
                # Style-Bert-VITS2 にある mora_list.py では VOICEVOX ENGINE のコードが若干改変された上で利用されているが、
                # "ッ" (促音) が Style-Bert-VITS2 版では cl から q に変わっているため、後述の update_length_and_pitch() 実行時に失敗しうる
                # これより前のコードで既に子音/母音が必要な処理は終えているため、ここで VOICEVOX ENGINE 向けに q を cl に修正する
                if mora.vowel == "q":
                    mora.vowel = "cl"
                moras.append(mora)

            # アクセント句を生成
            accent_phrases.append(
                AccentPhrase(
                    moras=moras,
                    # 取得したアクセント核を指定
                    accent=accent_index,
                    # AivisSpeech Engine では moras に記号モーラも含めているため、pause_mora は常に None に設定
                    ## Style-Bert-VITS2 は音声合成時に読み上げテキストに付与する記号 (…!? など)で感情表現が大きく変わるほか、
                    ## 記号自体にアクセント情報を付与できるため、VOICEVOX のように連続する記号を pause_mora としてまとめられると困る
                    pause_mora=None,
                )
            )

        # ダミーの音素長・モーラ音高を生成
        ## VOICEVOX ENGINE と異なりスタイル ID に基づいてその音素長・モーラ音高を更新することは原理上不可能なため、
        ## 音素長・モーラ音高は常にダミー値で返される
        ## 上記処理ですでにダミーデータは入れられているのだが、念のため
        accent_phrases = self.update_length_and_pitch(accent_phrases, style_id)
        return accent_phrases

    def update_length(
        self, accent_phrases: list[AccentPhrase], style_id: StyleId
    ) -> list[AccentPhrase]:
        """
        アクセント句系列に含まれるモーラの音素長属性をスタイルに合わせて更新する
        VOICEVOX ENGINE と異なりスタイル ID に基づいてその音素長を更新することは原理上不可能なため、常にダミー値が入る
        モック版 VOICEVOX CORE を使うとスタイル ID をベースに無意味に生成した巨大な値が音素長に設定されるため、敢えて使っていない
        """

        for accent_phrase in accent_phrases:
            for mora in accent_phrase.moras:
                # consonant_length は子音が None でない場合のみ更新
                if mora.consonant is not None:
                    mora.consonant_length = 0.0
                else:
                    mora.consonant_length = None
                # vowel_length は常にダミー値
                mora.vowel_length = 0.0
        return accent_phrases

    def update_pitch(
        self, accent_phrases: list[AccentPhrase], style_id: StyleId
    ) -> list[AccentPhrase]:
        """
        アクセント句系列に含まれるモーラの音高属性をスタイルに合わせて更新する
        VOICEVOX ENGINE と異なりスタイル ID に基づいてその音高を更新することは原理上不可能なため、常にダミー値が入る
        モック版 VOICEVOX CORE を使うとスタイル ID をベースに無意味に生成した巨大な値が音高に設定されるため、敢えて使っていない
        """

        for accent_phrase in accent_phrases:
            for mora in accent_phrase.moras:
                mora.pitch = 0.0
        return accent_phrases

    def synthesize_wave(
        self,
        query: AudioQuery,
        style_id: StyleId,
        enable_interrogative_upspeak: bool = True,
    ) -> NDArray[np.float32]:
        """
        音声合成用のクエリに含まれる読み仮名に基づいて Style-Bert-VITS2 で音声波形を生成する
        継承元の TTSEngine.synthesize_wave() をオーバーライドし、Style-Bert-VITS2 用の音声合成処理に差し替えている

        Parameters
        ----------
        query : AudioQuery
            音声合成用のクエリ
        style_id : StyleId
            スタイル ID
        enable_interrogative_upspeak : bool, optional
            疑問文の場合に抑揚を上げるかどうか (VOICEVOX ENGINE との互換性維持のためのパラメータ)

        Returns
        -------
        NDArray[np.float32]
            生成された音声波形 (float32 型)
        """

        # モーフィング時などに同一参照の AudioQuery で複数回呼ばれる可能性があるので、元の引数の AudioQuery に破壊的変更を行わない
        query = copy.deepcopy(query)

        # もし AudioQuery.kana に漢字混じりの通常の文章が指定されている場合はそれを使う (AivisSpeech 独自仕様)
        ## VOICEVOX ENGINE では AudioQuery.kana は読み取り専用パラメータだが、AivisSpeech Engine では
        ## 音声合成 API にアクセント句だけでなく通常の読み上げテキストを直接渡すためのパラメータとして利用している
        ## 通常の読み上げテキストの方が遥かに抑揚が自然になるため、読み仮名のみの読み上げテキストよりも優先される
        ## VOICEVOX ENGINE との互換性維持のための苦肉の策で、基本可能な限り AudioQuery.kana に読み上げテキストを指定すべき
        if query.kana is not None and query.kana != "":
            text = query.kana.strip()

            # アクセント辞書でのプレビュー時のエラーを回避するための処理
            ## もし AudioQuery に含まれる最後のモーラの text が "ガ" だったら、テキストの末尾に "ガ" を追加する
            ## Style-Bert-VITS2 ではトーンの数と g2p した際の音素の数が一致している必要があるが、
            ## アクセント辞書のプレビュー時にエディタから送信される kana の末尾には "ガ" が含まれておらず、InvalidToneError が発生する
            ## エディタ側で修正することも可能だが、VOICEVOX ENGINE との互換性のため、エラーにならないようにここで処理する
            if (
                len(query.accent_phrases) > 0
                and len(query.accent_phrases[-1].moras) > 0
            ):
                # 最後のモーラを取得し、その text が "ガ" であることを確認
                last_mora = query.accent_phrases[-1].moras[-1]
                if last_mora.text == "ガ":
                    # Style-Bert-VITS2 側の g2p 処理を呼び、カタカナ化されたモーラのリストを取得
                    kata_mora_list = g2kata_tone(normalize_text(text))
                    # kata_mora_list の最後のモーラが "ガ" でない場合は "ガ" を追加
                    if len(kata_mora_list) > 0 and kata_mora_list[-1][0] != "ガ":
                        text += "ガ"
        else:
            logger.warning("AudioQuery.kana is not specified. Using accent phrases instead.")  # fmt: skip
            # 読み仮名 (カタカナのみ) のテキストを取得
            ## ひらがなの方がまだ抑揚の棒読み度がマシになるため、カタカナをひらがなに変換した上で句点を付ける
            flatten_moras = to_flatten_moras(query.accent_phrases)
            text = "".join([mora.text for mora in flatten_moras]) + "。"
            text = jaconv.kata2hira(text)
            ## この時点で text が句点だけの場合は空文字列にする
            if text == "。":
                text = ""

        # AudioQuery.accent_phrase をカタカナモーラと音高 (0 or 1) のリストに変換
        kata_tone_list: list[tuple[str, int]] = []
        for accent_phrase in query.accent_phrases:
            # モーラのうちどこがアクセント核かを示すインデックス
            accent_index = accent_phrase.accent - 1  # 1-indexed -> 0-indexed
            for index, mora in enumerate(accent_phrase.moras):
                tone = 0
                # index が 0 かつ accent_index が 0 以外の時は常に tone を 0 にする
                if index == 0 and accent_index != 0:
                    tone = 0
                # index <= accent_index の時は tone を 1 にする
                elif index <= accent_index:
                    tone = 1
                # それ以外の時は tone を 0 にする
                else:
                    tone = 0
                # モーラのテキストと音高をリストに追加
                kata_tone_list.append((mora.text, tone))
            # もし pause_mora があればそれも追加
            ## AivisSpeech Engine から取得した AudioQuery がそのまま送られた際は pause_mora は設定されず、
            ## 句読点や記号は通常の mora (vowel=pau) として text 内の記号表現を維持した状態で含まれる
            ## 一方 AivisSpeech エディタ側で読みが編集されている際は AudioQuery に pause_mora が設定されていることがあるため、
            ## 互換性のために pause_mora が設定されている場合のみ読点として追加する
            if accent_phrase.pause_mora is not None:
                kata_tone_list.append((',', 0))  # テキストは "," 固定 ("," は正規化後の読点の文字列表現) 、音高は 0 固定  # fmt: skip

        # 音素と音高のリストに変換した後、さらにそれぞれ音素・音高だけのリストに変換
        ## text が空文字列の時は、InvalidToneError を回避するために None を渡す
        if text != "":
            # 事前にカタカナ表記でない音素と音高のリストに変換するのが大変重要
            ## これをやらないと InvalidToneError が発生する
            phone_tone_list = kata_tone2phone_tone(kata_tone_list)
            given_phone_list = [phone for phone, _ in phone_tone_list]
            given_tone_list = [tone for _, tone in phone_tone_list]
        else:
            given_phone_list = None
            given_tone_list = None

        # スタイル ID に対応する AivmManifest, AivmManifestSpeaker, AivmManifestSpeakerStyle を取得
        result = self.aivm_manager.get_aivm_manifest_from_style_id(style_id)
        aivm_manifest = result[0]
        aivm_manifest_speaker = result[1]
        aivm_manifest_speaker_style = result[2]

        # 音声合成モデルをロード (初回のみ)
        model = self.load_model(str(aivm_manifest.uuid))
        logger.info(f"Model: {aivm_manifest.name} / Version {aivm_manifest.version}")  # fmt: skip
        logger.info(f"Speaker: {aivm_manifest_speaker.name} / Style: {aivm_manifest_speaker_style.name} / Version {aivm_manifest_speaker.version}")  # fmt: skip

        # ローカルな話者 ID・スタイル ID を取得
        ## 現在の Style-Bert-VITS2 の API ではスタイル ID ではなくスタイル名を指定する必要があるため、
        ## 別途 local_style_id に対応するスタイル名をハイパーパラメータから取得している
        ## AIVM マニフェスト記載のスタイル名とハイパーパラメータのスタイル名は必ずしも一致しないため (通常一致するはずだが…) 、
        ## 万が一に備え AIVM マニフェストとハイパーパラメータで共通のスタイル ID からスタイル名を取得する
        local_speaker_id: int = aivm_manifest_speaker.local_id
        local_style_id: int = aivm_manifest_speaker_style.local_id
        local_style_name: str | None = None
        for hps_style_name, hps_style_id in model.hyper_parameters.data.style2id.items():  # fmt: skip
            if hps_style_id == local_style_id:
                local_style_name = hps_style_name
                break
        if local_style_name is None:
            raise ValueError(f"Style ID {local_style_id} not found in hyper parameters.")  # fmt: skip

        # 話速
        ## ref: https://github.com/litagin02/Style-Bert-VITS2/blob/2.4.1/server_editor.py#L314
        length = 1 / max(0.0, query.speedScale)

        # スタイルの強さ
        ## VOICEVOX では「抑揚」の比率だが、AivisSpeech では声のテンポの緩急を指定する値としている
        ## intonationScale の基準は 1.0 (0 ~ 2) なので、DEFAULT_STYLE_WEIGHT を基準とした 0 ~ 10 の範囲に変換する
        if 0.0 <= query.intonationScale <= 1.0:
            style_weight = query.intonationScale * DEFAULT_STYLE_WEIGHT
        elif 1.0 < query.intonationScale <= 2.0:
            style_weight = (
                DEFAULT_STYLE_WEIGHT
                + (query.intonationScale - 1.0) * (10.0 - DEFAULT_STYLE_WEIGHT) / 1.0
            )
        else:
            style_weight = DEFAULT_STYLE_WEIGHT

        # テンポの緩急 (SDP Ratio)
        ## Style-Bert-VITS2 にも一応「抑揚」パラメータはあるが、pyworld で変換している関係で明確に音質が劣化する上、あまり効果がない
        ## tempoDynamicsScale の基準は 1.0 (0 ~ 2) なので、DEFAULT_SDP_RATIO を基準とした 0 ~ 1 の範囲に変換する
        if 0.0 <= query.tempoDynamicsScale <= 1.0:
            sdp_ratio = query.tempoDynamicsScale * DEFAULT_SDP_RATIO
        elif 1.0 < query.tempoDynamicsScale <= 2.0:
            sdp_ratio = (
                DEFAULT_SDP_RATIO
                + (query.tempoDynamicsScale - 1.0) * (1.0 - DEFAULT_SDP_RATIO) / 1.0
            )
        else:
            sdp_ratio = DEFAULT_SDP_RATIO

        # ピッチ
        ## 0.0 以外を指定すると音質が劣化するので基本使わない
        ## pitchScale の基準は 0.0 (-1 ~ 1) なので、1.0 を基準とした 0 ~ 2 の範囲に変換する
        pitch_scale = max(0.0, 1.0 + query.pitchScale)

        # 音声合成を実行
        ## 出力音声は int16 型の NDArray で返される
        logger.info("Running inference...")
        logger.info(f"Text: {text}")
        logger.info(f"         Speed: {length:.2f} (Input: {query.speedScale:.2f})")
        logger.info(f"  Style Weight: {style_weight:.2f} (Input: {query.intonationScale:.2f})")  # fmt: skip
        logger.info(f"Tempo Dynamics: {sdp_ratio:.2f} (Input: {query.tempoDynamicsScale:.2f})")  # fmt: skip
        logger.info(f"         Pitch: {pitch_scale:.2f} (Input: {query.pitchScale:.2f})")  # fmt: skip
        logger.info(f"        Volume: {query.volumeScale:.2f}")
        logger.info(f"   Pre-Silence: {query.prePhonemeLength:.2f}")
        logger.info(f"  Post-Silence: {query.postPhonemeLength:.2f}")
        start_time = time.time()
        raw_sample_rate, raw_wave = model.infer(
            text=text,
            given_phone=given_phone_list,
            given_tone=given_tone_list,
            language=Languages.JP,
            speaker_id=local_speaker_id,
            style=local_style_name,
            style_weight=style_weight,
            sdp_ratio=sdp_ratio,
            length=length,
            pitch_scale=pitch_scale,
            # AivisSpeech Engine ではテキストの改行ごとの分割生成を行わない (エディタ側の機能と競合するため)
            # line_split=True だと音素やアクセントの指定ができない
            line_split=False,
        )
        logger.info("Inference done. Elapsed time: {:.2f} sec.".format(time.time() - start_time))  # fmt: skip

        # VOICEVOX CORE は float32 型の音声波形を返すため、int16 から float32 に変換して VOICEVOX CORE に合わせる
        ## float32 に変換する際に -1.0 ~ 1.0 の範囲に正規化する
        raw_wave = raw_wave.astype(np.float32) / 32768.0

        # 前後の無音区間を追加
        pre_silence_length = int(raw_sample_rate * query.prePhonemeLength)
        post_silence_length = int(raw_sample_rate * query.postPhonemeLength)
        silence_wave_pre = np.zeros(pre_silence_length, dtype=np.float32)
        silence_wave_post = np.zeros(post_silence_length, dtype=np.float32)
        raw_wave = np.concatenate((silence_wave_pre, raw_wave, silence_wave_post))

        # 生成した音声の音量調整/サンプルレート変更/ステレオ化を行ってから返す
        wave = raw_wave_to_output_wave(query, raw_wave, raw_sample_rate)
        return wave

    def initialize_synthesis(self, style_id: StyleId, skip_reinit: bool) -> None:
        """指定されたスタイル ID に関する合成機能を初期化する。既に初期化されていた場合は引数に応じて再初期化する。"""
        # スタイル ID に対応する AivmManifest を取得後、
        # AIVM マニフェスト記載の UUID に対応する音声合成モデルをロードする
        ## FIXME: StyleBertVITS2TTSEngine の内部実装上、当面 skip_reinit 引数は無視して必要なときのみロードする
        aivm_manifest, _, _ = self.aivm_manager.get_aivm_manifest_from_style_id(style_id)  # fmt: skip
        self.load_model(str(aivm_manifest.uuid))

    def is_synthesis_initialized(self, style_id: StyleId) -> bool:
        """指定されたスタイル ID に関する合成機能が初期化済みか否かを取得する。"""
        # スタイル ID に対応する AivmManifest を取得後、
        # AIVM マニフェスト記載の UUID に対応する音声合成モデルがロードされているかどうかを返す
        aivm_manifest, _, _ = self.aivm_manager.get_aivm_manifest_from_style_id(style_id)  # fmt: skip
        return self.is_model_loaded(str(aivm_manifest.uuid))
