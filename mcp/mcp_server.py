#!/usr/bin/env python3

import asyncio
import json
import logging
from typing import Any, Sequence, Dict, Optional
from datetime import datetime
import os
from contextlib import asynccontextmanager
import concurrent.futures
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import AzureChatOpenAI
from langchain_core.tools import BaseTool
from langchain_core.callbacks.manager import CallbackManagerForToolRun
from pydantic import Field

# Import MCP tools
from websocket_tool import TGXMarketDataTool
from jesse_tool import JesseHistoricalTool

# === Load Environment Variables ===
load_dotenv(dotenv_path=os.path.join("config", ".env"))

api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")
deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('mcp_master_server.log', mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# === Pydantic Models ===
class ChatRequest(BaseModel):
    messages: list

# === MCP Tool Wrappers ===
class MCPToolManager:
    def __init__(self):
        self.tgx_tool = TGXMarketDataTool()
        self.jesse_tool = JesseHistoricalTool()
        self.tools_initialized = False
        
    async def initialize(self):
        """Initialize MCP tools"""
        try:
            logger.info("🔧 Initializing MCP tools...")
            await self.tgx_tool.initialize()
            await self.jesse_tool.initialize()
            self.tools_initialized = True
            logger.info("✅ MCP tools initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize MCP tools: {e}")
            raise
    
    async def close(self):
        """Close MCP tools"""
        try:
            await self.tgx_tool.close()
            await self.jesse_tool.close()
            logger.info("✅ MCP tools closed successfully")
        except Exception as e:
            logger.error(f"❌ Error closing MCP tools: {e}")

# Global tool manager
tool_manager = None

# === Helper function to run async code in sync context ===
def run_async_safely(coro):
    """Safely run async code in sync context"""
    def run_in_thread():
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()
    
    try:
        # Try to get current loop
        loop = asyncio.get_running_loop()
        # If we're in an async context, run in a thread
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(run_in_thread)
            return future.result()
    except RuntimeError:
        # No running loop, safe to run directly
        return asyncio.run(coro)

# === Custom LangChain Tools ===
# === Custom LangChain Tools (FIXED VERSION) ===
class LiveCryptoDataTool(BaseTool):
    name: str = "get_live_crypto_data"
    description: str = """Get live cryptocurrency market data from TGX Finance WebSocket.
    
    Args:
        symbol: Cryptocurrency symbol (BTC, ETH, or 'all' for both)
    
    Returns:
        Current price, change, volume, and market data"""
    
    def _run(self, symbol: str = "BTC", run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_tool.get_market_data(symbol))
        except Exception as e:
            logger.error(f"Live crypto data tool error: {e}")
            return f"Error getting live data: {str(e)}"

class MarketStatusTool(BaseTool):
    name: str = "get_live_market_status"
    description: str = """Get TGX Finance WebSocket connection status and system health.
    
    Returns:
        Connection status, message count, authentication status"""
    
    def _run(self, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.tgx_tool.get_connection_status())
        except Exception as e:
            logger.error(f"Market status tool error: {e}")
            return f"Error getting market status: {str(e)}"

class HistoricalAnalysisTool(BaseTool):
    name: str = "get_crypto_historical_analysis"
    description: str = """Get historical cryptocurrency analysis from Jesse.ai database.
    
    Args:
        symbol: Cryptocurrency symbol (e.g., 'BTCUSDT', 'ETHUSDT')
        timeframe: Chart timeframe ('1m', '5m', '15m', '30m', '1h', '4h', '1D')
    
    Returns:
        Historical price analysis with percentage changes"""
    
    def _run(self, symbol: str, timeframe: str = "1D", run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.jesse_tool.get_historical_analysis(symbol, timeframe))
        except Exception as e:
            logger.error(f"Historical analysis tool error: {e}")
            return f"Error getting historical analysis: {str(e)}"

class HistoricalDataRangeTool(BaseTool):
    name: str = "get_crypto_historical_data_range"
    description: str = """Get historical cryptocurrency data for a specific date range from Jesse.ai database.
    
    Args:
        symbol: Cryptocurrency symbol (e.g., 'BTCUSDT', 'ETHUSDT')  
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        timeframe: Chart timeframe ('1m', '5m', '15m', '30m', '1h', '4h', '1D')
    
    Returns:
        Historical price data and analysis for the specified date range"""
    
    def _run(self, symbol: str, start_date: str, end_date: str, timeframe: str = "1D", 
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.jesse_tool.get_historical_data_range(symbol, start_date, end_date, timeframe))
        except Exception as e:
            logger.error(f"Historical data range tool error: {e}")
            return f"Error getting historical data range: {str(e)}"

class RawHistoricalDataTool(BaseTool):
    name: str = "get_crypto_raw_historical_data"
    description: str = """Get raw historical OHLCV data from Jesse.ai database.
    
    Args:
        symbol: Cryptocurrency symbol (e.g., 'BTCUSDT', 'ETHUSDT')
        timeframe: Chart timeframe ('1m', '5m', '15m', '30m', '1h', '4h', '1D')
        limit: Number of data points to return (default: 100)
    
    Returns:
        Raw OHLCV data in JSON format"""
    
    def _run(self, symbol: str, timeframe: str = "1D", limit: int = 100,
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            return run_async_safely(tool_manager.jesse_tool.get_raw_historical_data(symbol, timeframe, limit))
        except Exception as e:
            logger.error(f"Raw historical data tool error: {e}")
            return f"Error getting raw historical data: {str(e)}"

# === FastAPI App Setup ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown"""
    global tool_manager
    
    logger.info("🚀 Starting MCP Master Server...")
    
    tool_manager = MCPToolManager()
    await tool_manager.initialize()
    
    app.state.tool_manager = tool_manager
    
    logger.info("✅ MCP Master Server startup complete!")
    
    yield
    
    logger.info("🛑 Shutting down MCP Master Server...")
    if tool_manager:
        await tool_manager.close()
    logger.info("✅ Shutdown complete")

app = FastAPI(
    title="MCP Crypto Analysis Server",
    description="Master MCP server combining TGX Finance live data and Jesse.ai historical analysis",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# === API Endpoints ===
@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    """OpenAI-compatible chat completions endpoint with streaming"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="MCP tools not initialized")
        
        # Extract latest user message
        messages = request.messages
        if not messages:
            raise HTTPException(status_code=400, detail="Messages array required")
            
        last_user_msg = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
        if not last_user_msg:
            raise HTTPException(status_code=400, detail="No user message found")
            
        user_input = last_user_msg.get('content', '')
        if isinstance(user_input, list):
            user_input = ' '.join(item.get('text', '') for item in user_input if isinstance(item, dict))
        
        logger.info(f"💬 Chat request: '{user_input}'")
        
        def generate_response():
            try:
                # Create LLM
                client = AzureChatOpenAI(
                    azure_endpoint=endpoint,
                    azure_deployment=deployment,
                    openai_api_version=api_version,
                    api_key=azure_api_key,
                )
                
                # Create tool instances
                tools = [
                    LiveCryptoDataTool(),
                    MarketStatusTool(),
                    HistoricalAnalysisTool(),
                    HistoricalDataRangeTool(),
                    RawHistoricalDataTool()
                ]
                
                # Bind tools to client
                client_with_tools = client.bind_tools(tools)
                
                # Enhanced system prompt
                system_prompt = """You are a comprehensive crypto analysis assistant with access to:

1. **Live Market Data (TGX Finance WebSocket)**:
   - get_live_crypto_data: Real-time prices, changes, volume for BTC/ETH
   - get_live_market_status: WebSocket connection status and system health

2. **Historical Analysis (Jesse.ai Database)**:
   - get_crypto_historical_analysis: Performance analysis with timeframes
   - get_crypto_historical_data_range: Data for specific date ranges
   - get_crypto_raw_historical_data: Raw OHLCV data for charts

CRITICAL FORMATTING RULES:
- For price increases: Use <UP>**X.XX%** (increase)</UP>
- For price decreases: Use <DOWN>**X.XX%** (decrease)</DOWN>
- Always include "**" around the percentage and "(increase)" or "(decrease)" text

TOOL USAGE GUIDELINES:
- For "current price" or "live data" → use get_live_crypto_data
- For "system status" or "connection" → use get_live_market_status
- For "price history" or "performance" → use get_crypto_historical_analysis
- For specific dates (e.g., "August 2024") → use get_crypto_historical_data_range
- For chart data or raw OHLCV → use get_crypto_raw_historical_data

Always combine live and historical data when relevant to provide comprehensive analysis."""
                
                # System and user messages
                messages_with_tools = [
                    SystemMessage(system_prompt),
                    HumanMessage(user_input),
                ]
                
                # Run LangChain agent
                result = client_with_tools.invoke(messages_with_tools)
                messages_with_tools.append(result)
                
                # Process tool calls
                if result.tool_calls:
                    for tool_call in result.tool_calls:
                        # Log tool call info but DON'T emit to user
                        logger.info(f"🔧 Calling tool: {tool_call['name']} with args: {tool_call['args']}")
                        
                        # Find the matching tool
                        tool_found = None
                        for tool in tools:
                            if tool.name == tool_call["name"]:
                                tool_found = tool
                                break
                        
                        if tool_found:
                            try:
                                # Extract arguments from the tool call
                                tool_args = tool_call["args"]
                                
                                # Execute the tool with extracted arguments
                                tool_result = tool_found._run(**tool_args)
                                
                                tool_message = ToolMessage(
                                    content=tool_result,
                                    tool_call_id=tool_call["id"]
                                )
                                messages_with_tools.append(tool_message)
                            except Exception as e:
                                logger.error(f"Tool execution error: {e}")
                                error_message = ToolMessage(
                                    content=f"Tool execution error: {str(e)}",
                                    tool_call_id=tool_call["id"]
                                )
                                messages_with_tools.append(error_message)
                        else:
                            logger.warning(f"Tool not found: {tool_call['name']}")
                            error_message = ToolMessage(
                                content=f"Tool not found: {tool_call['name']}",
                                tool_call_id=tool_call["id"]
                            )
                            messages_with_tools.append(error_message)
                
                # Generate final response with streaming
                message_id = f"msg-{abs(hash(user_input)) % 10000}"
                
                for chunk in client_with_tools.stream(messages_with_tools):
                    if chunk.content:
                        chunk_data = {
                            "type": "text",
                            "id": message_id,
                            "data": chunk.content
                        }
                        yield f'{json.dumps([chunk_data])}\n'
                        
            except Exception as e:
                logger.error(f"Response generation error: {e}")
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
        
    except Exception as e:
        logger.error(f"Chat completions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/ask")
async def ask_endpoint(user_input: str):
    """Legacy ask endpoint for compatibility"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="MCP tools not initialized")
            
        # Create mock request
        mock_request = ChatRequest(messages=[{"role": "user", "content": user_input}])
        return await chat_completions(mock_request)
        
    except Exception as e:
        logger.error(f"Ask endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "services": {
                "mcp_tools": tool_manager.tools_initialized if tool_manager else False,
                "tgx_finance": "available" if tool_manager and tool_manager.tools_initialized else "unavailable",
                "jesse_database": "available" if tool_manager and tool_manager.tools_initialized else "unavailable",
                "azure_openai": "available" if azure_api_key else "unavailable"
            },
            "tools": [
                "get_live_crypto_data",
                "get_live_market_status", 
                "get_crypto_historical_analysis",
                "get_crypto_historical_data_range",
                "get_crypto_raw_historical_data"
            ]
        }
        
        if not tool_manager or not tool_manager.tools_initialized:
            health_status["status"] = "degraded"
            
        return health_status
        
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@app.get("/")
async def root():
    """API documentation root"""
    return {
        "title": "MCP Crypto Analysis Server",
        "version": "1.0.0", 
        "description": "Master MCP server combining TGX Finance live data and Jesse.ai historical analysis",
        "endpoints": {
            "/v1/chat/completions": "OpenAI-compatible chat completions with MCP tools (POST)",
            "/ask": "Legacy ask endpoint (GET with user_input parameter)",
            "/health": "Health check with service status",
        },
        "mcp_tools": [
            "get_live_crypto_data - Real-time market data from TGX Finance",
            "get_live_market_status - WebSocket connection status",
            "get_crypto_historical_analysis - Historical analysis from Jesse.ai",
            "get_crypto_historical_data_range - Date range historical data",
            "get_crypto_raw_historical_data - Raw OHLCV data"
        ],
        "supported_symbols": ["BTC", "ETH", "BTCUSDT", "ETHUSDT"],
        "supported_timeframes": ["1m", "5m", "15m", "30m", "1h", "4h", "1D"],
        "features": [
            "Live WebSocket market data",
            "Historical price analysis", 
            "Date range queries",
            "Raw OHLCV data",
            "Streaming responses",
            "Tool call transparency",
            "Proper async/sync handling with custom BaseTool classes"
        ]
    }

if __name__ == "__main__":
    import uvicorn
    
    try:
        port = int(os.getenv("PORT", "8000"))
        logger.info(f"🚀 Starting MCP Master Server on port {port}")
        
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            timeout_keep_alive=60,
            reload=False,
            access_log=True
        )
        
    except Exception as e:
        logger.error(f"❌ Failed to start server: {e}")
        raise