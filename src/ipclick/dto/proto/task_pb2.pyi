from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper

DESCRIPTOR: _descriptor.FileDescriptor

class AdapterType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CURL_CFFI: _ClassVar[AdapterType]
    HTTPX: _ClassVar[AdapterType]
    REQUESTS: _ClassVar[AdapterType]
    DRISSIONPAGE: _ClassVar[AdapterType]
    UC: _ClassVar[AdapterType]
    PLAYWRIGHT: _ClassVar[AdapterType]

class HttpMethod(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    GET: _ClassVar[HttpMethod]
    POST: _ClassVar[HttpMethod]
    PUT: _ClassVar[HttpMethod]
    DELETE: _ClassVar[HttpMethod]
    PATCH: _ClassVar[HttpMethod]
    HEAD: _ClassVar[HttpMethod]
    OPTIONS: _ClassVar[HttpMethod]
    TRACE: _ClassVar[HttpMethod]

CURL_CFFI: AdapterType
HTTPX: AdapterType
REQUESTS: AdapterType
DRISSIONPAGE: AdapterType
UC: AdapterType
PLAYWRIGHT: AdapterType
GET: HttpMethod
POST: HttpMethod
PUT: HttpMethod
DELETE: HttpMethod
PATCH: HttpMethod
HEAD: HttpMethod
OPTIONS: HttpMethod
TRACE: HttpMethod

class ReqTask(_message.Message):
    __slots__ = (
        "adapter",
        "allow_redirects",
        "allowed_status_codes",
        "automation_config",
        "automation_script",
        "cookies",
        "data",
        "extensions",
        "headers",
        "impersonate",
        "json",
        "kwargs",
        "max_retries",
        "method",
        "params",
        "proxy",
        "retry_backoff_seconds",
        "stream",
        "timeout_seconds",
        "url",
        "uuid",
        "verify_ssl",
    )
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...

    class CookiesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...

    class ExtensionsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...

    UUID_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    HEADERS_FIELD_NUMBER: _ClassVar[int]
    COOKIES_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    PROXY_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    MAX_RETRIES_FIELD_NUMBER: _ClassVar[int]
    RETRY_BACKOFF_SECONDS_FIELD_NUMBER: _ClassVar[int]
    VERIFY_SSL_FIELD_NUMBER: _ClassVar[int]
    ALLOW_REDIRECTS_FIELD_NUMBER: _ClassVar[int]
    STREAM_FIELD_NUMBER: _ClassVar[int]
    IMPERSONATE_FIELD_NUMBER: _ClassVar[int]
    EXTENSIONS_FIELD_NUMBER: _ClassVar[int]
    AUTOMATION_CONFIG_FIELD_NUMBER: _ClassVar[int]
    AUTOMATION_SCRIPT_FIELD_NUMBER: _ClassVar[int]
    ALLOWED_STATUS_CODES_FIELD_NUMBER: _ClassVar[int]
    KWARGS_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    adapter: AdapterType
    method: HttpMethod
    url: str
    headers: _containers.ScalarMap[str, str]
    cookies: _containers.ScalarMap[str, str]
    params: str
    data: str
    json: str
    proxy: str
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    verify_ssl: bool
    allow_redirects: bool
    stream: bool
    impersonate: str
    extensions: _containers.ScalarMap[str, str]
    automation_config: str
    automation_script: str
    allowed_status_codes: _containers.RepeatedScalarFieldContainer[int]
    kwargs: str
    def __init__(
        self,
        uuid: str | None = ...,
        adapter: AdapterType | str | None = ...,
        method: HttpMethod | str | None = ...,
        url: str | None = ...,
        headers: _Mapping[str, str] | None = ...,
        cookies: _Mapping[str, str] | None = ...,
        params: str | None = ...,
        data: str | None = ...,
        json: str | None = ...,
        proxy: str | None = ...,
        timeout_seconds: float | None = ...,
        max_retries: int | None = ...,
        retry_backoff_seconds: float | None = ...,
        verify_ssl: bool | None = ...,
        allow_redirects: bool | None = ...,
        stream: bool | None = ...,
        impersonate: str | None = ...,
        extensions: _Mapping[str, str] | None = ...,
        automation_config: str | None = ...,
        automation_script: str | None = ...,
        allowed_status_codes: _Iterable[int] | None = ...,
        kwargs: str | None = ...,
    ) -> None: ...

class TaskResp(_message.Message):
    __slots__ = (
        "adapter",
        "content",
        "effective_url",
        "error_message",
        "original_request",
        "request_uuid",
        "response_headers",
        "response_time_ms",
        "status_code",
    )
    class ResponseHeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: str | None = ..., value: str | None = ...) -> None: ...

    REQUEST_UUID_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    ORIGINAL_REQUEST_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_URL_FIELD_NUMBER: _ClassVar[int]
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_HEADERS_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    request_uuid: str
    adapter: AdapterType
    original_request: ReqTask
    effective_url: str
    status_code: int
    response_headers: _containers.ScalarMap[str, str]
    content: bytes
    error_message: str
    response_time_ms: int
    def __init__(
        self,
        request_uuid: str | None = ...,
        adapter: AdapterType | str | None = ...,
        original_request: ReqTask | _Mapping | None = ...,
        effective_url: str | None = ...,
        status_code: int | None = ...,
        response_headers: _Mapping[str, str] | None = ...,
        content: bytes | None = ...,
        error_message: str | None = ...,
        response_time_ms: int | None = ...,
    ) -> None: ...
