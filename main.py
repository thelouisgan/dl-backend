from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import os
import json
import asyncio
import websockets
from typing import Dict, Optional
from dotenv import load_dotenv

from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI

# === Load Environment Variables ===
load_dotenv(dotenv_path=os.path.join("config", ".env"))

api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

# === Setup FastAPI app ===
app = FastAPI()

# === Qubit WebSocket Configuration ===
QUBIT_WS_URL = "wss://api.tgx.finance/v1/ws/"

# Global storage for market data
market_data: Dict[str, Dict] = {}
ws_connection = None


class QubitWebSocketClient:
    def __init__(self):
        self.websocket = None
        self.is_connected = False

    async def connect(self):
        try:
        # Simple connection without extra_headers for compatibility
            self.websocket = await websockets.connect(QUBIT_WS_URL)
            self.is_connected = True
            print("Successfully connected to Qubit WebSocket")

        # Subscribe to market data - try different subscription formats
            subscribe_message = {
                "action": "sub",
                "data": {},
                "topic": "contracts.market"
            }
            await self.websocket.send(json.dumps(subscribe_message))
            print(f"Sent subscription message: {subscribe_message}")

        # Also try alternative subscription formats
            alt_subscribe = {
                "action": "subscribe", 
                "topic": "market.ticker"
            }
            await self.websocket.send(json.dumps(alt_subscribe))
            print(f"Sent alternative subscription: {alt_subscribe}")

        # Start listening for messages
            asyncio.create_task(self.listen_for_messages())

        except Exception as e:
            print(f"Failed to connect to Qubit WebSocket: {e}")
            self.is_connected = False
            
            self.is_connected = True
            print("Successfully connected to Qubit WebSocket")

            # Subscribe to market data - let's try different subscription formats
            # First try the original format
            subscribe_message = {
                "action": "sub",
                "data": {},
                "topic": "contracts.market"
            }
            await self.websocket.send(json.dumps(subscribe_message))
            print(f"Sent subscription message: {subscribe_message}")

            # Also try alternative subscription formats that might be needed
            alt_subscribe = {
                "action": "subscribe",
                "topic": "market.ticker"
            }
            await self.websocket.send(json.dumps(alt_subscribe))
            print(f"Sent alternative subscription: {alt_subscribe}")

            # Start listening for messages
            asyncio.create_task(self.listen_for_messages())

        except Exception as e:
            print(f"Failed to connect to Qubit WebSocket: {e}")
            self.is_connected = False

    async def listen_for_messages(self):
        """Listen for incoming WebSocket messages and store market data"""
        global market_data
        
        try:
            async for message in self.websocket:
                try:
                    print(f"Raw WebSocket message: {message}")  # Debug
                    data = json.loads(message)
                    print(f"Parsed WebSocket data: {data}")  # Debug
                    
                    # Check what topics/structure we're actually getting
                    topic = data.get("topic") or data.get("type") or data.get("event")
                    print(f"Message topic/type: {topic}")
                    
                    # Handle different possible message formats
                    if topic == "market.ticker" or topic == "ticker":
                        ticker_data = data.get("data", data)
                        print(f"Processing ticker data: {ticker_data}")
                        
                        # Look for contract/symbol identifiers
                        contract_code = (ticker_data.get("contract_code") or 
                                       ticker_data.get("symbol") or 
                                       ticker_data.get("pair"))
                        
                        if contract_code:
                            market_data[contract_code] = ticker_data
                            print(f"Stored ticker data for {contract_code}")

                    elif topic == "contract.applies" or topic == "price":
                        contract_data = data.get("data", data)
                        print(f"Processing contract/price data: {contract_data}")
                        
                        contract_code = (contract_data.get("contract_code") or 
                                       contract_data.get("symbol") or 
                                       contract_data.get("pair"))
                        
                        if contract_code:
                            if contract_code in market_data:
                                market_data[contract_code].update(contract_data)
                            else:
                                market_data[contract_code] = contract_data
                            print(f"Updated price data for {contract_code}")
                    
                    # Handle any other message types
                    else:
                        print(f"Unhandled message type '{topic}': {data}")
                        # Store raw data to see what we're getting
                        if "symbol" in data or "contract_code" in data or "pair" in data:
                            symbol = data.get("symbol") or data.get("contract_code") or data.get("pair")
                            market_data[f"raw_{symbol}"] = data
                            print(f"Stored raw data for {symbol}")

                except json.JSONDecodeError as e:
                    print(f"Failed to parse WebSocket message: {message}, Error: {e}")
                except Exception as e:
                    print(f"Error processing message: {e}")

        except websockets.exceptions.ConnectionClosed:
            print("WebSocket connection closed")
            self.is_connected = False
        except Exception as e:
            print(f"Error in WebSocket listener: {e}")
            self.is_connected = False

    async def disconnect(self):
        """Disconnect from WebSocket"""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            print("Disconnected from WebSocket")


# Initialize WebSocket client
qubit_client = QubitWebSocketClient()


@app.on_event("startup")
async def startup_event():
    """Connect to Qubit WebSocket on startup"""
    await qubit_client.connect()


@app.on_event("shutdown")
async def shutdown_event():
    """Disconnect from WebSocket on shutdown"""
    await qubit_client.disconnect()


def normalize_symbol(symbol: str) -> str:
    """Normalize symbol format (e.g., BTC/USDT -> BTCUSDT)"""
    return symbol.upper().replace("/", "")


@tool
def get_crypto_price(symbol: str) -> str:
    """
    Get the current price of a cryptocurrency from Qubit.
    Example symbols: 'BTC/USDT', 'ETH/USDT', 'BTCUSDT'
    """
    try:
        normalized_symbol = normalize_symbol(symbol)

        if normalized_symbol not in market_data:
            return f"No price data available for {symbol}. Available symbols: {list(market_data.keys())}"

        data = market_data[normalized_symbol]

        # Try to get price from various possible fields
        price = None
        if "last_price" in data:
            price = data["last_price"]
        elif "price" in data:
            price = data["price"]
        elif "mark_price" in data:
            price = data["mark_price"]

        if price is not None:
            return f"The current price of {symbol.upper()} is {price} USDT."
        else:
            return f"Price data found for {symbol} but no price field available. Data: {data}"

    except Exception as e:
        return f"Error fetching price for {symbol}: {e}"


@tool
def get_crypto_info(symbol: str) -> str:
    """
    Get detailed ticker info for a cryptocurrency pair from Qubit.
    Example symbols: 'BTC/USDT', 'ETH/USDT', 'BTCUSDT'
    """
    try:
        normalized_symbol = normalize_symbol(symbol)

        if normalized_symbol not in market_data:
            return f"No data available for {symbol}. Available symbols: {list(market_data.keys())}"

        data = market_data[normalized_symbol]

        # Build comprehensive info string
        info_lines = [f"Symbol: {symbol.upper()}"]

        # Add available data fields
        field_mappings = {
            "last_price": "Last Price",
            "price": "Price",
            "mark_price": "Mark Price",
            "high_price": "24h High",
            "low_price": "24h Low",
            "change": "24h Change",
            "change_ratio": "24h Change %",
            "volume": "Volume",
            "turnover": "Turnover"
        }

        for field, label in field_mappings.items():
            if field in data:
                value = data[field]
                if field == "change_ratio":
                    info_lines.append(f"{label}: {value}%")
                else:
                    info_lines.append(f"{label}: {value}")

        return "\n".join(info_lines)

    except Exception as e:
        return f"Error fetching details for {symbol}: {e}"


# === Streaming API Endpoint ===
@app.get("/ask")
async def ask_stream(user_input: str):
    def generate_response():
        try:
            # Create LLM
            client = AzureChatOpenAI(
                azure_endpoint=endpoint,
                azure_deployment=deployment,
                openai_api_version=api_version,
                api_key=azure_api_key,
            )
            client_with_tools = client.bind_tools([get_crypto_info, get_crypto_price])

            # System and user prompt
            messages_with_tools = [
                SystemMessage("You are a helpful assistant that can pull current crypto data from Qubit exchange."),
                HumanMessage(user_input),
            ]

            # Run LangChain agent to check for tool calls
            result = client_with_tools.invoke(messages_with_tools)
            messages_with_tools.append(result)

            # Process tool calls if any
            if result.tool_calls:
                for tool_call in result.tool_calls:
                    # Emit tool call info - properly escape JSON
                    tool_data = {
                        "type": "tool_call",
                        "id": tool_call["id"],
                        "name": tool_call["name"],
                        "args": tool_call["args"]
                    }
                    yield f'{json.dumps([tool_data])}\n'

                    # Execute tool
                    tool_fn = {
                        "get_crypto_info": get_crypto_info,
                        "get_crypto_price": get_crypto_price
                    }[tool_call["name"]]

                    tool_result = tool_fn.invoke(tool_call)
                    tool_message = ToolMessage(
                        content=tool_result,
                        tool_call_id=tool_call["id"]
                    )
                    messages_with_tools.append(tool_message)

            # Generate final response with streaming
            message_id = f"msg-{abs(hash(user_input)) % 10000}"

            # First, let's collect all streaming chunks to avoid broken JSON
            collected_content = ""
            for chunk in client_with_tools.stream(messages_with_tools):
                if chunk.content:
                    collected_content += chunk.content
                    # Send each character/token individually but as complete JSON
                    chunk_data = {
                        "type": "text",
                        "id": message_id,
                        "data": chunk.content
                    }
                    yield f'{json.dumps([chunk_data])}\n'

        except Exception as e:
            error_msg = f"Error: {str(e)}"
            error_data = {
                "type": "text",
                "id": f"error-{abs(hash(str(e))) % 1000}",
                "data": error_msg
            }
            yield f'{json.dumps([error_data])}\n'

    return StreamingResponse(
        generate_response(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


# === Non-streaming endpoint for compatibility ===
@app.get("/ask_simple")
async def ask_simple(user_input: str):
    try:
        # Create LLM
        client = AzureChatOpenAI(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            openai_api_version=api_version,
            api_key=azure_api_key,
        )
        client_with_tools = client.bind_tools([get_crypto_info, get_crypto_price])

        # System and user prompt
        messages_with_tools = [
            SystemMessage("You are a helpful assistant that can pull current crypto data from Qubit exchange."),
            HumanMessage(user_input),
        ]

        # Run LangChain agent
        result = client_with_tools.invoke(messages_with_tools)
        messages_with_tools.append(result)

        tool_outputs = []
        if result.tool_calls:
            for tool_call in result.tool_calls:
                tool_fn = {
                    "get_crypto_info": get_crypto_info,
                    "get_crypto_price": get_crypto_price
                }[tool_call["name"]]

                tool_result = tool_fn.invoke(tool_call)
                tool_outputs.append(tool_result)

                tool_message = ToolMessage(
                    content=tool_result,
                    tool_call_id=tool_call["id"]
                )
                messages_with_tools.append(tool_message)

        # Final response
        final_response = client_with_tools.invoke(messages_with_tools)

        return {
            "response": final_response.content,
            "tool_outputs": tool_outputs
        }

    except Exception as e:
        return {
            "error": str(e),
            "response": f"Error: {str(e)}",
            "tool_outputs": []
        }


# === Debug endpoint to see available market data ===
@app.get("/market_data")
async def get_market_data():
    """Debug endpoint to see what market data is available"""
    return {
        "connected": qubit_client.is_connected,
        "available_symbols": list(market_data.keys()),
        "sample_data": {k: v for k, v in list(market_data.items())[:3]}  # Show first 3 entries
    }