import json
import logging
import uuid
from abc import ABC, abstractmethod
from threading import Event, Thread
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from httpx_sse import connect_sse, EventSource


class McpClient(ABC):
    """Interface for MCP client."""

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def initialize(self):
        raise NotImplementedError

    @abstractmethod
    def list_tools(self):
        raise NotImplementedError

    @abstractmethod
    def call_tool(self, tool_name: str, tool_args: dict):
        raise NotImplementedError


def remove_request_params(url: str) -> str:
    return urljoin(url, urlparse(url).path)


class McpSseClient(McpClient):
    """
    HTTP with SSE transport MCP client.
    """

    def __init__(self, name: str, url: str,
                 headers: dict[str, Any] | None = None,
                 timeout: float = 50,
                 sse_read_timeout: float = 50,
                 ):
        self.name = name
        self.url = url
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        self.endpoint_url = None
        self.client = httpx.Client(headers=headers)
        self.message_dict = {}
        self.response_ready = Event()
        self.should_stop = Event()
        self._listen_thread = None
        self._connected = Event()
        self._error_event = Event()
        self._thread_exception = None
        self.connect()

    def _listen_messages(self) -> None:
        try:
            logging.info(f"{self.name} - Connecting to SSE endpoint: {remove_request_params(self.url)}")
            with connect_sse(
                    client=self.client,
                    method="GET",
                    url=self.url,
                    timeout=httpx.Timeout(self.timeout, read=self.sse_read_timeout),
            ) as event_source:
                event_source.response.raise_for_status()
                logging.debug(f"{self.name} - SSE connection established")
                for sse in event_source.iter_sse():
                    logging.debug(f"{self.name} - Received SSE event: {sse.event}")
                    if self.should_stop.is_set():
                        break
                    match sse.event:
                        case "endpoint":
                            self.endpoint_url = urljoin(self.url, sse.data)
                            logging.info(f"{self.name} - Received endpoint URL: {self.endpoint_url}")
                            self._connected.set()
                            url_parsed = urlparse(self.url)
                            endpoint_parsed = urlparse(self.endpoint_url)
                            if (url_parsed.netloc != endpoint_parsed.netloc
                                    or url_parsed.scheme != endpoint_parsed.scheme):
                                error_msg = f"{self.name} - Endpoint origin does not match connection origin: {self.endpoint_url}"
                                logging.error(error_msg)
                                raise ValueError(error_msg)
                        case "message":
                            message = json.loads(sse.data)
                            logging.debug(f"{self.name} - Received server message: {message}")
                            self.message_dict[message["id"]] = message
                            self.response_ready.set()
                        case _:
                            logging.warning(f"{self.name} - Unknown SSE event: {sse.event}")
        except Exception as e:
            self._thread_exception = e
            self._error_event.set()
            self._connected.set()

    def send_message(self, data: dict):
        if not self.endpoint_url:
            if self._thread_exception:
                raise ConnectionError(f"{self.name} - MCP Server connection failed: {self._thread_exception}")
            else:
                raise RuntimeError(f"{self.name} - Please call connect() first")
        logging.debug(f"{self.name} - Sending client message: {data}")
        response = self.client.post(
            url=self.endpoint_url,
            json=data,
            headers={'Content-Type': 'application/json', 'trace-id': data["id"] if "id" in data else ""},
            timeout=self.timeout,
            follow_redirects=True
        )
        response.raise_for_status()
        logging.debug(f"{self.name} - Client message sent successfully: {response.status_code}")
        if "id" in data:
            message_id = data["id"]
            while True:
                self.response_ready.wait()
                self.response_ready.clear()
                if message_id in self.message_dict:
                    message = self.message_dict.pop(message_id, None)
                    return message
        return {}

    def connect(self) -> None:
        self._listen_thread = Thread(target=self._listen_messages, daemon=True)
        self._listen_thread.start()
        while True:
            if self._error_event.is_set():
                if isinstance(self._thread_exception, httpx.HTTPStatusError):
                    raise ConnectionError(f"{self.name} - MCP Server connection failed: {self._thread_exception}") \
                        from self._thread_exception
                else:
                    raise self._thread_exception
            if self._connected.wait(timeout=0.1):
                break
            if not self._listen_thread.is_alive():
                raise ConnectionError(f"{self.name} - MCP Server SSE listener thread died unexpectedly!")

    def close(self) -> None:
        try:
            self.should_stop.set()
            self.client.close()
            if self._listen_thread and self._listen_thread.is_alive():
                self._listen_thread.join(timeout=10)
        except Exception as e:
            raise Exception(f"{self.name} - MCP Server connection close failed: {str(e)}")

    def initialize(self):
        init_data = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "MCP HTTP with SSE Client",
                    "version": "1.0.0"
                }
            }
        }
        response = self.send_message(init_data)
        if "error" in response:
            raise Exception(f"MCP Server initialize error: {response['error']}")
        notify_data = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }
        response = self.send_message(notify_data)
        if "error" in response:
            raise Exception(f"MCP Server notifications/initialized error: {response['error']}")

    def list_tools(self):
        tools_data = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/list",
            "params": {}
        }
        response = self.send_message(tools_data)
        if "error" in response:
            raise Exception(f"MCP Server tools/list error: {response['error']}")
        return response.get("result", {}).get("tools", [])

    def call_tool(self, tool_name: str, tool_args: dict):
        call_data = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": tool_args
            }
        }
        response = self.send_message(call_data)
        if "error" in response:
            raise Exception(f"MCP Server tools/call error: {response['error']}")
        return response.get("result", {}).get("content", [])


class McpStreamableHttpClient(McpClient):
    """
    Streamable HTTP transport MCP client.
    """

    def __init__(self, name: str, url: str,
                 headers: dict[str, Any] | None = None,
                 timeout: float = 50,
                 ):
        self.name = name
        self.url = url
        self.timeout = timeout
        self.client = httpx.Client(headers=headers)
        self.session_id = None

    def close(self) -> None:
        try:
            self.client.close()
        except Exception as e:
            raise Exception(f"{self.name} - MCP Server connection close failed: {str(e)}")

    def send_message(self, data: dict):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        logging.debug(f"{self.name} - Sending client message: {data}")
        response = self.client.post(
            url=self.url,
            json=data,
            headers=headers,
            timeout=self.timeout,
            follow_redirects=True
        )
        self.session_id = response.headers.get("mcp-session-id", None)
        content_type = response.headers.get("content-type", "None")
        if content_type == "text/event-stream":
            for sse in EventSource(response).iter_sse():
                if sse.event != "message":
                    raise Exception(f"{self.name} - Unknown Server-Sent Event: {sse.event}")
                return json.loads(sse.data)
        elif content_type == "application/json":
            return response.json() if response.content else {}
        else:
            raise Exception(f"{self.name} - Unsupported Content-Type: {content_type}")

    def initialize(self):
        init_data = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "MCP Streamable HTTP Client",
                    "version": "1.0.0"
                }
            }
        }
        response = self.send_message(init_data)
        if "error" in response:
            raise Exception(f"MCP Server initialize error: {response['error']}")
        notify_data = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }
        response = self.send_message(notify_data)
        if "error" in response:
            raise Exception(f"MCP Server notifications/initialized error: {response['error']}")

    def list_tools(self):
        tools_data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {}
        }
        response = self.send_message(tools_data)
        if "error" in response:
            raise Exception(f"MCP Server tools/list error: {response['error']}")
        return response.get("result", {}).get("tools", [])

    def call_tool(self, tool_name: str, tool_args: dict):
        call_data = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": tool_args
            }
        }
        response = self.send_message(call_data)
        if "error" in response:
            raise Exception(f"MCP Server tools/call error: {response['error']}")
        return response.get("result", {}).get("content", [])


class McpClients:
    def __init__(self, servers_config: dict[str, Any]):
        if "mcpServers" in servers_config:
            servers_config = servers_config["mcpServers"]
        self._clients = {
            name: self.init_client(name, config)
            for name, config in servers_config.items()
        }
        for client in self._clients.values():
            client.initialize()
        self._tools = {}

    @staticmethod
    def init_client(name: str, config: dict[str, Any]) -> McpClient:
        transport = "sse"
        if "transport" in config:
            transport = config["transport"]
        if transport == "streamable_http":
            return McpStreamableHttpClient(
                name=name,
                url=config.get("url"),
                headers=config.get("headers", None),
                timeout=config.get("timeout", 50),
            )
        return McpSseClient(
            name=name,
            url=config.get("url"),
            headers=config.get("headers", None),
            timeout=config.get("timeout", 50),
            sse_read_timeout=config.get("sse_read_timeout", 50),
        )

    def fetch_tools(self) -> list[dict]:
        try:
            all_tools = []
            for server_name, client in self._clients.items():
                tools = client.list_tools()
                all_tools.extend(tools)
                self._tools[server_name] = tools
            return all_tools
        except Exception as e:
            raise RuntimeError(f"Error fetching tools: {str(e)}")

    def execute_tool(self, tool_name: str, tool_args: dict[str, Any]):
        if not self._tools:
            self.fetch_tools()
        tool_clients = {}
        for server_name, tools in self._tools.items():
            for tool in tools:
                if server_name in self._clients:
                    tool_clients[tool["name"]] = self._clients[server_name]
        client = tool_clients.get(tool_name, None)
        try:
            if not client:
                raise Exception(f"there is not a tool named {tool_name}")
            result = client.call_tool(tool_name, tool_args)
            if isinstance(result, dict) and "progress" in result:
                progress = result["progress"]
                total = result["total"]
                percentage = (progress / total) * 100
                logging.info(
                    f"Progress: {progress}/{total} "
                    f"({percentage:.1f}%)"
                )
            return f"Tool execution result: {result}"
        except Exception as e:
            error_msg = f"Error executing tool: {str(e)}"
            logging.error(error_msg)
            return error_msg

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception as e:
                logging.error(e)
