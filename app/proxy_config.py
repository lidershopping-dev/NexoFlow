from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import quote

from project_paths import load_private_settings

secrets = load_private_settings()

VALID_MODES = {"auto", "on", "off"}
DIRECT_TEST_URL = "https://pypi.org/simple/pip/"
PROXY_DETECTION_ATTEMPTS = 3
PROXY_RETRY_DELAY_SECONDS = 0.35

# Uma troca de rede pode ser percebida simultaneamente por vários workers.
# Serializamos a redetecção para evitar múltiplas sondagens/proxy flips concorrentes.
_ROUTE_REFRESH_LOCK = threading.Lock()


@dataclass(frozen=True)
class ProxyStatus:
    mode: str
    configured: bool
    reachable: bool
    active: bool
    host: str
    port: int
    has_credentials: bool
    reason: str

    @property
    def safe_summary(self) -> str:
        endpoint = (
            f"{self.host}:{self.port}" if self.host and self.port else "não configurado"
        )
        auth = "sim" if self.has_credentials else "não"
        active = "sim" if self.active else "não"
        reachable = "sim" if self.reachable else "não"
        return (
            f"modo={self.mode} | proxy={endpoint} | "
            f"acessível={reachable} | ativo={active} | "
            f"credenciais={auth} | {self.reason}"
        )


def _read_settings() -> tuple[str, str, int, str, str, str, float]:
    mode = str(getattr(secrets, "PROXY_MODE", "off")).strip().lower()
    host = str(getattr(secrets, "PROXY_HOST", "")).strip()

    raw_port = getattr(secrets, "PROXY_PORT", 0)
    try:
        port = int(raw_port or 0)
    except (TypeError, ValueError):
        raise ValueError("PROXY_PORT precisa ser um número inteiro.") from None

    user = str(getattr(secrets, "PROXY_USER", "")).strip()
    password = str(getattr(secrets, "PROXY_PASSWORD", ""))
    bypass = str(
        getattr(
            secrets,
            "PROXY_BYPASS",
            "localhost,127.0.0.1",
        )
    ).strip()

    raw_timeout = getattr(secrets, "PROXY_DETECTION_TIMEOUT", 1.5)
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        timeout = 1.5

    if mode not in VALID_MODES:
        raise ValueError("PROXY_MODE deve ser 'auto', 'on' ou 'off'.")

    if bool(user) != bool(password):
        raise ValueError(
            "Preencha PROXY_USER e PROXY_PASSWORD juntos, ou deixe os dois vazios."
        )

    return mode, host, port, user, password, bypass, timeout


def _is_reachable(host: str, port: int, timeout: float) -> bool:
    if not host or not port:
        return False

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _direct_internet_available(timeout: float) -> bool:
    """
    Testa internet direta ignorando proxy do Windows/ambiente.

    Isso é importante porque urllib/pip podem herdar o proxy configurado
    no Windows mesmo quando o nosso código decidiu não ativá-lo.
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            DIRECT_TEST_URL,
            method="HEAD",
            headers={"User-Agent": "NexoFlow/1.0"},
        )
        with opener.open(request, timeout=max(timeout, 3.0)) as response:
            return 200 <= getattr(response, "status", 200) < 500
    except (OSError, HTTPException):
        # URLError, erros TLS e timeouts são subclasses de OSError.
        # Erros de programação não devem ser confundidos com falta de rede.
        return False


class _NoProbeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _direct_farmalider_available(timeout: float) -> bool:
    """Probe the external service over HTTPS, without tokens or customer data.

    An unrelated public site being reachable does not establish access to the
    external FarmaLíder port. Do not follow redirects to login/block pages.
    """
    from config import FARMALIDER_API_BASE_URL_EXTERNAL

    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoProbeRedirect()
        )
        request = urllib.request.Request(
            FARMALIDER_API_BASE_URL_EXTERNAL,
            method="HEAD",
            headers={"User-Agent": "NexoFlow/1.0"},
        )
        with opener.open(request, timeout=max(timeout, 3.0)) as response:
            return 200 <= response.status < 300
    except HTTPError as exc:
        # These responses establish reachability without credentials. A blocked
        # route (403/407), redirect, timeout, throttling or 5xx is not proof.
        return exc.code in {401, 404, 405}
    except (OSError, HTTPException):
        return False


def _build_proxy_url(
    host: str,
    port: int,
    user: str,
    password: str,
) -> str:
    if not host or not port:
        raise ValueError("Proxy sem host/porta configurados.")

    if user and password:
        safe_user = quote(user, safe="")
        safe_password = quote(password, safe="")
        return f"http://{safe_user}:{safe_password}@{host}:{port}"

    return f"http://{host}:{port}"


def _clear_project_proxy_env() -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
    ):
        os.environ.pop(name, None)


def _force_direct_mode() -> None:
    """
    Evita que requests/pip voltem a usar silenciosamente o proxy do Windows.
    """
    _clear_project_proxy_env()
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    os.environ["WENI_PROXY_ACTIVE"] = "0"


def _activate_proxy(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    bypass: str,
) -> None:
    proxy_url = _build_proxy_url(
        host=host,
        port=port,
        user=user,
        password=password,
    )

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
    ):
        os.environ[name] = proxy_url

    os.environ["WENI_PROXY_ACTIVE"] = "1"

    # Se antes estávamos em modo direto, remove o "*" e volta para a
    # lista normal de exceções.
    if bypass:
        os.environ["NO_PROXY"] = bypass
        os.environ["no_proxy"] = bypass
    else:
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)


def configure_proxy(
    *, detection_attempts: int | None = None, preserve_active_proxy: bool = False
) -> ProxyStatus:
    """
    Configura proxy somente para o processo Python atual.

    auto:
      1. Se host:porta responder, ativa o proxy autenticado.
      2. Se o teste TCP falhar, testa internet DIRETA ignorando o proxy
         configurado no Windows.
      3. Se a internet direta funcionar, força conexão direta de verdade.
      4. Se a internet direta NÃO funcionar e há credenciais, ativa o
         proxy mesmo que o teste TCP tenha dado falso negativo.

    on:
      força o proxy configurado.

    off:
      força conexão direta e impede fallback silencioso para o proxy
      configurado no Windows.
    """
    (
        mode,
        host,
        port,
        user,
        password,
        bypass,
        timeout,
    ) = _read_settings()

    configured = bool(host and port)
    has_credentials = bool(user and password)

    if mode == "off":
        _force_direct_mode()
        return ProxyStatus(
            mode=mode,
            configured=configured,
            reachable=False,
            active=False,
            host=host,
            port=port,
            has_credentials=has_credentials,
            reason="proxy desativado; conexão direta forçada",
        )

    if not configured:
        if mode == "on":
            raise ValueError(
                "PROXY_MODE='on', mas PROXY_HOST/PROXY_PORT não foram configurados."
            )

        _force_direct_mode()
        return ProxyStatus(
            mode=mode,
            configured=False,
            reachable=False,
            active=False,
            host=host,
            port=port,
            has_credentials=has_credentials,
            reason="proxy não configurado; conexão direta forçada",
        )

    reachable = False
    attempts = (
        PROXY_DETECTION_ATTEMPTS
        if detection_attempts is None
        else max(1, int(detection_attempts))
    )
    for attempt in range(attempts):
        if _is_reachable(host, port, timeout):
            reachable = True
            break
        if attempt < attempts - 1:
            time.sleep(PROXY_RETRY_DELAY_SECONDS)

    if mode == "on":
        _activate_proxy(
            host=host,
            port=port,
            user=user,
            password=password,
            bypass=bypass,
        )
        return ProxyStatus(
            mode=mode,
            configured=True,
            reachable=reachable,
            active=True,
            host=host,
            port=port,
            has_credentials=has_credentials,
            reason="proxy forçado pela configuração",
        )

    # AUTO
    if reachable:
        _activate_proxy(
            host=host,
            port=port,
            user=user,
            password=password,
            bypass=bypass,
        )
        return ProxyStatus(
            mode=mode,
            configured=True,
            reachable=True,
            active=True,
            host=host,
            port=port,
            has_credentials=has_credentials,
            reason="proxy detectado automaticamente e ativado",
        )

    # O teste TCP pode dar falso negativo em algumas redes corporativas.
    # Antes de concluir que estamos fora da empresa, testamos internet
    # realmente direta, sem usar o proxy do Windows.
    direct_available = _direct_internet_available(timeout)

    if preserve_active_proxy and (
        not direct_available or not _direct_farmalider_available(timeout)
    ):
        _activate_proxy(
            host=host, port=port, user=user, password=password, bypass=bypass
        )
        return ProxyStatus(
            mode=mode,
            configured=True,
            reachable=False,
            active=True,
            host=host,
            port=port,
            has_credentials=has_credentials,
            reason=(
                "sondagens do proxy falharam, mas o acesso direto à FarmaLíder "
                "não foi comprovado; mantendo o proxy já ativo"
            ),
        )

    if direct_available:
        _force_direct_mode()
        return ProxyStatus(
            mode=mode,
            configured=True,
            reachable=False,
            active=False,
            host=host,
            port=port,
            has_credentials=has_credentials,
            reason=(
                "proxy não respondeu ao teste, mas internet direta funciona; "
                "conexão direta forçada"
            ),
        )

    if has_credentials:
        _activate_proxy(
            host=host,
            port=port,
            user=user,
            password=password,
            bypass=bypass,
        )
        return ProxyStatus(
            mode=mode,
            configured=True,
            reachable=False,
            active=True,
            host=host,
            port=port,
            has_credentials=True,
            reason=(
                "teste TCP do proxy falhou e internet direta não funcionou; "
                "ativando proxy autenticado como fallback"
            ),
        )

    _force_direct_mode()
    return ProxyStatus(
        mode=mode,
        configured=True,
        reachable=False,
        active=False,
        host=host,
        port=port,
        has_credentials=False,
        reason=(
            "proxy não respondeu e não há credenciais configuradas; "
            "conexão direta forçada"
        ),
    )


@dataclass(frozen=True)
class NetworkRouteRefresh:
    status: ProxyStatus
    changed: bool
    previous_active: bool | None
    current_active: bool

    @property
    def transition(self) -> str:
        before = (
            "proxy"
            if self.previous_active is True
            else "direta"
            if self.previous_active is False
            else "desconhecida"
        )
        after = "proxy" if self.current_active else "direta"
        return f"{before} -> {after}"


def _proxy_active_from_env() -> bool | None:
    value = os.getenv("WENI_PROXY_ACTIVE", "").strip()
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def refresh_network_route(*, reason: str = "falha de rede") -> NetworkRouteRefresh:
    """
    Redetecta a rota durante uma execução já em andamento.

    É acionada após uma falha de rede recuperável. Assim uma carga longa pode
    migrar direta <-> proxy sem reiniciar o processo. Um proxy já ativo só é
    abandonado após as sondagens TCP e a confirmação da URL externa FarmaLíder;
    acesso a um site público isolado não comprova essa rota.
    """
    with _ROUTE_REFRESH_LOCK:
        previous_active = _proxy_active_from_env()
        status = configure_proxy(preserve_active_proxy=previous_active is True)
        current_active = bool(status.active)
        changed = previous_active is not None and previous_active != current_active

        if changed:
            logging.warning(
                "NexoFlow detectou mudança de rede durante a execução (%s): %s. "
                "A operação atual será repetida na nova rota.",
                reason,
                "direta -> proxy" if current_active else "proxy -> direta",
            )
        else:
            logging.info(
                "NexoFlow revalidou a rota após %s; mantendo conexão %s. %s.",
                reason,
                "via proxy" if current_active else "direta",
                status.reason,
            )

        return NetworkRouteRefresh(
            status=status,
            changed=changed,
            previous_active=previous_active,
            current_active=current_active,
        )
