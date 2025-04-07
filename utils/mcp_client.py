import json
import logging
import time
from queue import Queue, Empty
from threading import Event, Thread
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from httpx_sse import connect_sse


def remove_request_params(url: str) -> str:
    return urljoin(url, urlparse(url).path)


class McpClient:
    def __init__(self, url: str,
                 headers: dict[str, Any] | None = None,
                 timeout: float = 60,
                 sse_read_timeout: float = 60 * 5,
                 ):
        self.url = url
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        self.endpoint_url = None
        self.client = httpx.Client(headers=headers)
        self._request_id = 0
        self.message_queue = Queue()
        self.response_ready = Event()
        self.should_stop = Event()
        self._listen_thread = None
        self._connected = Event()
        self.connect()

    def _listen_messages(self) -> None:
        logging.info(f"Connecting to SSE endpoint: {remove_request_params(self.url)}")
        with connect_sse(
                client=self.client,
                method="GET",
                url=self.url,
                timeout=httpx.Timeout(self.timeout, read=self.sse_read_timeout),
        ) as event_source:
            event_source.response.raise_for_status()
            logging.debug("SSE connection established")
            for sse in event_source.iter_sse():
                logging.debug(f"Received SSE event: {sse.event}")
                match sse.event:
                    case "endpoint":
                        self.endpoint_url = urljoin(self.url, sse.data)
                        logging.info(f"Received endpoint URL: {self.endpoint_url}")
                        self._connected.set()
                        url_parsed = urlparse(self.url)
                        endpoint_parsed = urlparse(self.endpoint_url)
                        if (url_parsed.netloc != endpoint_parsed.netloc
                                or url_parsed.scheme != endpoint_parsed.scheme):
                            error_msg = f"Endpoint origin does not match connection origin: {self.endpoint_url}"
                            logging.error(error_msg)
                            raise ValueError(error_msg)
                    case "message":
                        message = json.loads(sse.data)
                        logging.debug(f"Received server message: {message}")
                        self.message_queue.put(message)
                        self.response_ready.set()
                    case _:
                        logging.warning(f"Unknown SSE event: {sse.event}")

    def send_message(self, data: dict):
        if not self.endpoint_url:
            raise RuntimeError("please call connect() first")
        response = self.client.post(
            url=self.endpoint_url,
            json=data,
            headers={'Content-Type': 'application/json'},
            timeout=self.timeout
        )
        response.raise_for_status()
        if "id" in data:
            message_id = data["id"]
            while True:
                self.response_ready.wait()
                self.response_ready.clear()
                try:
                    while True:
                        message = self.message_queue.get_nowait()
                        if "id" in message and message["id"] == message_id:
                            self._request_id += 1
                            return message
                        self.message_queue.put(message)
                except Empty:
                    pass
        return {}

    def connect(self) -> None:
        self._listen_thread = Thread(target=self._listen_messages, daemon=True)
        self._listen_thread.start()
        if not self._connected.wait(timeout=self.timeout):
            raise TimeoutError("MCP Server connection timeout!")

    def close(self) -> None:
        self.should_stop.is_set()
        self.client.close()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=10)

    def initialize(self):
        init_data = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "mcp",
                    "version": "0.1.0"
                }
            }
        }
        self.send_message(init_data)
        notify_data = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }
        self.send_message(notify_data)
        time.sleep(0.1)

    def list_tools(self):
        tools_data = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/list",
            "params": {}
        }
        return self.send_message(tools_data).get("result", {}).get("tools", [])

    def call_tool(self, tool_name: str, tool_args: dict):
        call_data = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": tool_args
            }
        }
        return self.send_message(call_data).get("result", {}).get("content", [])


def init_clients(servers_config: dict[str, Any]) -> list[McpClient]:
    if "mcpServers" in servers_config:
        servers_config = servers_config["mcpServers"]
    return [
        McpClient(
            url=config.get("url"),
            headers=config.get("headers", None),
            timeout=config.get("timeout", 60),
            sse_read_timeout=config.get("sse_read_timeout", 300),
        )
        for name, config in servers_config.items()
    ]


class McpClientsUtil:

    @staticmethod
    def fetch_tools(servers_config: dict[str, Any]) -> list[dict]:
        all_tools = []
        for client in init_clients(servers_config):
            try:
                client.initialize()
                tools = client.list_tools()
            finally:
                client.close()
            all_tools.extend(tools)
        return all_tools

    @staticmethod
    def execute_tool(servers_config: dict[str, Any], tool_name: str, tool_args: dict[str, Any]):
        for client in init_clients(servers_config):
            try:
                client.initialize()
                tools = client.list_tools()
            except Exception as e:
                client.close()
                error_msg = f"Error initialize or list tools: {str(e)}"
                logging.error(error_msg)
                continue
            if any(tool.get("name") == tool_name for tool in tools):
                try:
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
                finally:
                    client.close()
