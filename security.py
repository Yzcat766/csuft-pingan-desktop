"""Windows current-user DPAPI via pywin32; no plaintext fallback."""

import base64
import copy


class CredentialError(ValueError):
    pass


class DPAPI:
    def protect(self, value):
        try:
            import win32crypt

            return base64.b64encode(
                win32crypt.CryptProtectData(
                    value.encode("utf-8"),
                    "CSUFT-Pingan credentials",
                    None,
                    None,
                    None,
                    1,
                )
            ).decode("ascii")
        except Exception:
            raise CredentialError(
                "Windows 用户凭据加密失败；原配置未替换。请检查当前用户和 pywin32 运行环境。"
            ) from None

    def unprotect(self, value):
        try:
            import win32crypt

            return win32crypt.CryptUnprotectData(
                base64.b64decode(value, validate=True), None, None, None, 1
            )[1].decode("utf-8")
        except Exception:
            raise CredentialError(
                "凭据无法解密：请使用原 Windows 用户/电脑，或恢复密码加密备份，或重新编辑账号输入 OpenID。原密文仍保留。"
            ) from None


def encode_config(data, protector=None):
    protector = protector or DPAPI()
    value = copy.deepcopy(data)
    value["version"] = 3
    for account in value["accounts"]:
        secret = account.pop("openid", "")
        if secret:
            account["openid_protected"] = {
                "scheme": "dpapi-user-v1",
                "value": protector.protect(secret),
            }
            account.pop("credential_error", None)
        elif not account.get("openid_protected"):
            raise CredentialError("账号缺少凭据，请编辑或移除无效账号后再保存。")
    return value


def decode_config(data, protector=None):
    protector = protector or DPAPI()
    value = copy.deepcopy(data)
    for account in value["accounts"]:
        encrypted = account.get("openid_protected")
        if encrypted:
            try:
                if encrypted.get("scheme") != "dpapi-user-v1":
                    raise CredentialError("凭据保护版本不兼容，请更新软件。")
                account["openid"] = protector.unprotect(encrypted["value"])
                account.pop("credential_error", None)
                account.pop("openid_protected", None)
            except (CredentialError, KeyError, TypeError) as error:
                account["openid"] = ""
                account["credential_error"] = (
                    str(error)
                    if isinstance(error, CredentialError)
                    else "凭据格式损坏，请重新编辑账号或恢复备份。"
                )
                account["enabled"] = False
    return value
