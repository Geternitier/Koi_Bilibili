"""B 站业务错误。调用方把它变成给她看的一句人话，不把响应体原样抛出去。"""


class BiliError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PluginError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


RISK_CODES = frozenset({-352, -412, -509})
ALREADY_CODES = {
    65006: "已经点过赞了",
    11201: "已经收藏过了",
    34005: "这个视频的投币到上限了",
    12051: "这条评论已经发过了",
}


def public_message(error: BiliError) -> str:
    if error.code in ALREADY_CODES:
        return ALREADY_CODES[error.code]
    known = {
        -101: "B站登录过期了，需要主人帮我重新登录",
        -352: "B站风控拦住了，先停一段时间",
        -412: "B站把这次请求拦截了，先停一段时间",
        -509: "B站说请求太频繁了，先停一段时间",
        12002: "评论区关了",
        12052: "评论区关了",
        12016: "B站觉得这句话里有敏感信息",
    }
    if error.code in known:
        return known[error.code]
    text = error.message.strip() or "B站没有答应"
    return text[:80]
