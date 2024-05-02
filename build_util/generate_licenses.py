import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Literal


class LicenseError(Exception):
    # License違反があった場合、このエラーを出します。
    pass


class License:
    def __init__(
        self,
        name: str,  # TODO: `package_name` へリネーム
        version: str | None,  # TODO: `package_version` へリネーム
        license: str | None,  # TODO: `license_name` へリネーム
        text: str,  # TODO: `license_text` へリネーム
        license_text_type: Literal["raw", "local_address", "remote_address"],
    ):
        self.name = name  # TODO: `package_name` へリネーム
        self.version = version  # TODO: `package_version` へリネーム
        self.license = license  # TODO: `license_name` へリネーム

        if license_text_type == "raw":
            self.text = text  # TODO: `license_text` へリネーム
        elif license_text_type == "local_address":
            # ライセンステキストをローカルのライセンスファイルから抽出する
            self.text = Path(text).read_text(encoding="utf8")
        elif license_text_type == "remote_address":
            # ライセンステキストをリモートのライセンスファイルから抽出する
            with urllib.request.urlopen(text) as res:
                license_text: str = res.read().decode()
                self.text = license_text
        else:
            raise Exception("型で保護され実行されないはずのパスが実行されました")


def generate_licenses() -> list[License]:
    licenses: list[License] = []

    licenses += [
        License(
            name="VOICEVOX ENGINE",
            version=None,
            license="LGPL license",
            text="https://raw.githubusercontent.com/VOICEVOX/voicevox_engine/master/LGPL_LICENSE",
            license_text_type="remote_address",
        ),
        # https://sourceforge.net/projects/open-jtalk/files/Open%20JTalk/open_jtalk-1.11/
        License(
            name="Open JTalk",
            version="1.11",
            license="Modified BSD license",
            text="docs/licenses/open_jtalk/COPYING",
            license_text_type="local_address",
        ),
        License(
            name="MeCab",
            version=None,
            license="Modified BSD license",
            text="docs/licenses/open_jtalk/mecab/COPYING",
            license_text_type="local_address",
        ),
        License(
            name="NAIST Japanese Dictionary",
            version=None,
            license="Modified BSD license",
            text="docs/licenses//open_jtalk/mecab-naist-jdic/COPYING",
            license_text_type="local_address",
        ),
        License(
            name="PyTorch",
            version="2.2.2",
            license="BSD-style license",
            text="https://raw.githubusercontent.com/pytorch/pytorch/master/LICENSE",
            license_text_type="remote_address",
        ),
    ]
    python_version = "3.11.9"
    licenses += [
        License(
            name="Python",
            version=python_version,
            license="Python Software Foundation License",
            text=f"https://raw.githubusercontent.com/python/cpython/v{python_version}/LICENSE",
            license_text_type="remote_address",
        )
    ]

    # pip
    try:
        pip_licenses_output = subprocess.run(
            "pip-licenses "
            "--from=mixed "
            "--format=json "
            "--with-urls "
            "--with-license-file "
            "--no-license-path ",
            shell=True,
            capture_output=True,
            check=True,
            env=os.environ,
        ).stdout.decode()
    except subprocess.CalledProcessError as err:
        raise Exception(
            f"command output:\n{err.stderr and err.stderr.decode()}"
        ) from err

    licenses_json = json.loads(pip_licenses_output)
    for license_json in licenses_json:
        license = License(
            name=license_json["Name"],
            version=license_json["Version"],
            license=license_json["License"],
            text=license_json["LicenseText"],
            license_text_type="raw",
        )
        # FIXME: assert license type
        if license.text == "UNKNOWN":
            if license.name.lower() == "future":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/PythonCharmers/python-future/master/LICENSE.txt"  # noqa: B950
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "pefile":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/erocarrera/pefile/master/LICENSE"  # noqa: B950
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "pyopenjtalk-dict":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/litagin02/pyopenjtalk/master/LICENSE.md"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "python-multipart":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/andrew-d/python-multipart/master/LICENSE.txt"  # noqa: B950
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "romkan":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/soimort/python-romkan/master/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "distlib":
                with urllib.request.urlopen(
                    "https://bitbucket.org/pypa/distlib/raw/7d93712134b28401407da27382f2b6236c87623a/LICENSE.txt"  # noqa: B950
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "jsonschema":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/python-jsonschema/jsonschema/dbc398245a583cb2366795dc529ae042d10c1577/COPYING"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "lockfile":
                with urllib.request.urlopen(
                    "https://opendev.org/openstack/pylockfile/raw/tag/0.12.2/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "platformdirs":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/platformdirs/platformdirs/aa671aaa97913c7b948567f4d9c77d4f98bfa134/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "webencodings":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/gsnedders/python-webencodings/fa2cb5d75ab41e63ace691bc0825d3432ba7d694/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "antlr4-python3-runtime":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/antlr/antlr4/v4.11.1/LICENSE.txt"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "gradio_client":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/gradio-app/gradio/v3.41.0/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "jieba":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/fxsjy/jieba/v0.42.1/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "primepy":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/janaindrajit/primePy/9c98276fee5211e8761dfc03c9a1e02127e09e4a/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "pyproject_hooks":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/pypa/pyproject-hooks/v1.1.0/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "safetensors":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/huggingface/safetensors/v0.4.3/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "sentencepiece":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/google/sentencepiece/v0.2.0/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "tokenizers":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/huggingface/tokenizers/v0.19.1/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            elif license.name.lower() == "types-pyyaml":
                with urllib.request.urlopen(
                    "https://raw.githubusercontent.com/python/typeshed/57f3dcac8dbed008479b251512975901a0206deb/LICENSE"
                ) as res:
                    license.text = res.read().decode()
            else:
                # ライセンスがpypiに無い
                raise Exception(f"No License info provided for {license.name}")

        # soxr
        if license.name.lower() == "soxr":
            with urllib.request.urlopen(
                "https://raw.githubusercontent.com/dofuuz/python-soxr/v0.3.6/LICENSE.txt"
            ) as res:
                license.text = res.read().decode()

        licenses.append(license)

    return licenses


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output_path", type=str)
    args = parser.parse_args()

    output_path = args.output_path

    licenses = generate_licenses()

    # dump
    out = Path(output_path).open("w") if output_path else sys.stdout
    json.dump(
        [
            {
                "name": license.name,
                "version": license.version,
                "license": license.license,
                "text": license.text,
            }
            for license in licenses
        ],
        out,
    )
