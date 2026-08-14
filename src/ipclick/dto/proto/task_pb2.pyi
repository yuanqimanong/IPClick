from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AdapterType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CURL_CFFI: _ClassVar[AdapterType]
    HTTPX: _ClassVar[AdapterType]
    REQUESTS: _ClassVar[AdapterType]
    DRISSIONPAGE: _ClassVar[AdapterType]
    UC: _ClassVar[AdapterType]
    PLAYWRIGHT: _ClassVar[AdapterType]
    CAMOUFOX: _ClassVar[AdapterType]
    PATCHRIGHT: _ClassVar[AdapterType]
    BROWSER: _ClassVar[AdapterType]
    NIQUESTS: _ClassVar[AdapterType]

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
CAMOUFOX: AdapterType
PATCHRIGHT: AdapterType
BROWSER: AdapterType
NIQUESTS: AdapterType
GET: HttpMethod
POST: HttpMethod
PUT: HttpMethod
DELETE: HttpMethod
PATCH: HttpMethod
HEAD: HttpMethod
OPTIONS: HttpMethod
TRACE: HttpMethod

class ReqTask(_message.Message):
    __slots__ = ("uuid", "adapter", "method", "url", "headers", "cookies", "params", "data", "json", "proxy", "timeout_seconds", "max_retries", "retry_backoff_seconds", "verify_ssl", "allow_redirects", "stream", "impersonate", "automation_config", "automation_script", "allowed_status_codes", "kwargs")
    class HeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class CookiesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
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
    data: bytes
    json: str
    proxy: str
    timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    verify_ssl: bool
    allow_redirects: bool
    stream: bool
    impersonate: str
    automation_config: str
    automation_script: str
    allowed_status_codes: _containers.RepeatedScalarFieldContainer[int]
    kwargs: str
    def __init__(self, uuid: _Optional[str] = ..., adapter: _Optional[_Union[AdapterType, str]] = ..., method: _Optional[_Union[HttpMethod, str]] = ..., url: _Optional[str] = ..., headers: _Optional[_Mapping[str, str]] = ..., cookies: _Optional[_Mapping[str, str]] = ..., params: _Optional[str] = ..., data: _Optional[bytes] = ..., json: _Optional[str] = ..., proxy: _Optional[str] = ..., timeout_seconds: _Optional[float] = ..., max_retries: _Optional[int] = ..., retry_backoff_seconds: _Optional[float] = ..., verify_ssl: _Optional[bool] = ..., allow_redirects: _Optional[bool] = ..., stream: _Optional[bool] = ..., impersonate: _Optional[str] = ..., automation_config: _Optional[str] = ..., automation_script: _Optional[str] = ..., allowed_status_codes: _Optional[_Iterable[int]] = ..., kwargs: _Optional[str] = ...) -> None: ...

class TaskResp(_message.Message):
    __slots__ = ("request_uuid", "adapter", "effective_url", "status_code", "response_headers", "content", "error_message", "response_time_ms", "trace")
    class ResponseHeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    REQUEST_UUID_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_URL_FIELD_NUMBER: _ClassVar[int]
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_HEADERS_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    TRACE_FIELD_NUMBER: _ClassVar[int]
    request_uuid: str
    adapter: AdapterType
    effective_url: str
    status_code: int
    response_headers: _containers.ScalarMap[str, str]
    content: bytes
    error_message: str
    response_time_ms: int
    trace: Trace
    def __init__(self, request_uuid: _Optional[str] = ..., adapter: _Optional[_Union[AdapterType, str]] = ..., effective_url: _Optional[str] = ..., status_code: _Optional[int] = ..., response_headers: _Optional[_Mapping[str, str]] = ..., content: _Optional[bytes] = ..., error_message: _Optional[str] = ..., response_time_ms: _Optional[int] = ..., trace: _Optional[_Union[Trace, _Mapping]] = ...) -> None: ...

class Trace(_message.Message):
    __slots__ = ("node_id", "adapter", "attempts", "forwarded", "queued_ms")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    FORWARDED_FIELD_NUMBER: _ClassVar[int]
    QUEUED_MS_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    adapter: str
    attempts: int
    forwarded: bool
    queued_ms: int
    def __init__(self, node_id: _Optional[str] = ..., adapter: _Optional[str] = ..., attempts: _Optional[int] = ..., forwarded: _Optional[bool] = ..., queued_ms: _Optional[int] = ...) -> None: ...

class TaskRespHeader(_message.Message):
    __slots__ = ("request_uuid", "adapter", "effective_url", "status_code", "response_headers", "error_message", "content_length")
    class ResponseHeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    REQUEST_UUID_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_FIELD_NUMBER: _ClassVar[int]
    EFFECTIVE_URL_FIELD_NUMBER: _ClassVar[int]
    STATUS_CODE_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_HEADERS_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_LENGTH_FIELD_NUMBER: _ClassVar[int]
    request_uuid: str
    adapter: AdapterType
    effective_url: str
    status_code: int
    response_headers: _containers.ScalarMap[str, str]
    error_message: str
    content_length: int
    def __init__(self, request_uuid: _Optional[str] = ..., adapter: _Optional[_Union[AdapterType, str]] = ..., effective_url: _Optional[str] = ..., status_code: _Optional[int] = ..., response_headers: _Optional[_Mapping[str, str]] = ..., error_message: _Optional[str] = ..., content_length: _Optional[int] = ...) -> None: ...

class TaskRespChunk(_message.Message):
    __slots__ = ("header", "chunk", "trailer")
    HEADER_FIELD_NUMBER: _ClassVar[int]
    CHUNK_FIELD_NUMBER: _ClassVar[int]
    TRAILER_FIELD_NUMBER: _ClassVar[int]
    header: TaskRespHeader
    chunk: bytes
    trailer: TaskRespTrailer
    def __init__(self, header: _Optional[_Union[TaskRespHeader, _Mapping]] = ..., chunk: _Optional[bytes] = ..., trailer: _Optional[_Union[TaskRespTrailer, _Mapping]] = ...) -> None: ...

class TaskRespTrailer(_message.Message):
    __slots__ = ("response_time_ms", "total_bytes", "error_message")
    RESPONSE_TIME_MS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_BYTES_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    response_time_ms: int
    total_bytes: int
    error_message: str
    def __init__(self, response_time_ms: _Optional[int] = ..., total_bytes: _Optional[int] = ..., error_message: _Optional[str] = ...) -> None: ...
