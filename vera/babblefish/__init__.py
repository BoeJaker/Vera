"""
babblefish — Vera's universal network-protocol translator.

Named for the Babel fish of *The Hitchhiker's Guide to the Galaxy*: pop one in
your ear and you instantly understand any language. Babblefish does the same for
network protocols — it lets the LLM speak (encode), hear (decode) and converse
over arbitrary wire protocols through a registry of small, pluggable protocol
modules.

Public surface lives in `modules.py` (the protocol framework + built-ins) and is
exposed to the agent as the `babblefish.*` capability group defined in
`babblefish_capabilities.py`.
"""

from .modules import (           # noqa: F401
    ProtocolModule,
    DeclarativeModule,
    ConnectionContext,
    REGISTRY,
    SESSIONS,
    PROFILES,
    get_module,
    list_modules,
    register_declarative,
    tcp_roundtrip,
    udp_roundtrip,
    get_profile,
    learn_profile,
    open_session,
    get_session,
    close_session,
    list_sessions,
)
