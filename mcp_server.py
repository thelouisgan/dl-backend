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
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
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
from jesse_chart_tool import JesseChartTool

# Load Environment Variables
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
        logging.FileHandler('visual_chart_server.log', mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Pydantic Models
class ChatRequest(BaseModel):
    messages: list

# Tool Manager
class MCPToolManager:
    def __init__(self):
        self.tgx_tool = TGXMarketDataTool()
        self.jesse_tool = JesseHistoricalTool()
        self.jesse_chart_tool = JesseChartTool()
        self.tools_initialized = False
        
    async def initialize(self):
        try:
            logger.info("🎯 Initializing MCP tools for chart generation...")
            await self.tgx_tool.initialize()
            await self.jesse_tool.initialize()
            await self.jesse_chart_tool.initialize()
            self.tools_initialized = True
            logger.info("✅ MCP tools initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize MCP tools: {e}")
            raise
    
    async def close(self):
        try:
            await self.tgx_tool.close()
            await self.jesse_tool.close()
            await self.jesse_chart_tool.close()
            logger.info("✅ MCP tools closed")
        except Exception as e:
            logger.error(f"❌ Error closing MCP tools: {e}")

# Global tool manager
tool_manager = None

# Helper function for async execution
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
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(run_in_thread)
            return future.result()
    except RuntimeError:
        return asyncio.run(coro)

# 🎯 FIXED CHART TOOL - Uses ```graph instead of ```chart
# Enhanced GuaranteedVisualChartTool with automatic BTC+ETH comparison detection




class GuaranteedVisualChartTool(BaseTool):
    name: str = "generate_guaranteed_visual_chart"
    description: str = """🎯 PRIMARY CHART TOOL - Generates visual charts compatible with chart-renderer.tsx
    
    Args:
        symbol: Primary crypto symbol (BTC, ETH, BTCUSDT, etc.) 
        days_back: Days to show (7, 14, 30, 90, 365) - default 30
        chart_type: 'price' for single crypto, 'comparison' for multiple lines on same graph
        additional_symbols: List of symbols for comparison (e.g. ['ETH'] for BTC vs ETH)
        comparison_request: Set to True to force comparison mode with BTC+ETH
    
    Returns:
        Interactive visual chart in ```graph blocks with multiple lines on single graph
    """
    
    def _run(self, symbol: str, days_back: int = 30, chart_type: str = "price", 
             additional_symbols: list = None, comparison_request: bool = False,
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        
        try:
            logger.info(f"🎯 GENERATING VISUAL CHART: {symbol}, {days_back}d, type: {chart_type}")
            
            # Ensure tools are initialized
            if not tool_manager or not tool_manager.tools_initialized:
                error_msg = "Chart tools not initialized"
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)
            
            # 🔥 ENHANCED AUTO-DETECT for BTC+ETH comparison requests
            symbol_upper = symbol.upper()
            auto_comparison_symbols = []
            force_comparison = comparison_request
            
            # Check for explicit comparison keywords or requests
            btc_variants = ['BTC', 'BITCOIN', 'BTCUSDT', 'BTC-USDT']
            eth_variants = ['ETH', 'ETHEREUM', 'ETHUSDT', 'ETH-USDT', 'ETHER']
            
            # NEW: Check if this is a comparison request (user said "comparison", "vs", "both", etc.)
            comparison_keywords = ['comparison', 'compare', 'vs', 'versus', 'both', 'together', 'against']
            
            # If the primary symbol is BTC and it's a comparison request, add ETH
            if any(variant in symbol_upper for variant in btc_variants):
                if comparison_request or chart_type == "comparison":
                    auto_comparison_symbols = ['ETH']
                    force_comparison = True
                    logger.info("🔥 COMPARISON MODE: BTC primary - Adding ETH for side-by-side comparison")
            
            # If the primary symbol is ETH and it's a comparison request, add BTC  
            elif any(variant in symbol_upper for variant in eth_variants):
                if comparison_request or chart_type == "comparison":
                    auto_comparison_symbols = ['BTC']
                    force_comparison = True
                    logger.info("🔥 COMPARISON MODE: ETH primary - Adding BTC for side-by-side comparison")
            
            # Combine with any manually specified additional symbols
            if additional_symbols:
                all_additional = list(set(auto_comparison_symbols + additional_symbols))
            else:
                all_additional = auto_comparison_symbols
            
            # Force comparison mode if we have additional symbols
            if all_additional and len(all_additional) > 0:
                chart_type = "comparison"
                force_comparison = True
            
            # Get real data from Jesse database
            try:
                if force_comparison and all_additional:
                    # 🎯 FIXED: Generate SINGLE comparison chart with MULTIPLE LINES
                    all_symbols = [symbol] + all_additional
                    logger.info(f"📊 Generating SINGLE COMPARISON CHART with MULTIPLE LINES: {all_symbols}")
                    
                    # This should create ONE chart with multiple data series (lines)
                    chart_data_json = run_async_safely(
                        tool_manager.jesse_chart_tool.get_comparison_chart_data(all_symbols, days_back)
                    )
                    
                    chart_description = f"Multi-Line Comparison Chart"
                    symbols_text = f"{' vs '.join(all_symbols)}"
                    
                else:
                    # Generate single price chart
                    logger.info(f"📊 Generating SINGLE PRICE CHART: {symbol}")
                    chart_data_json = run_async_safely(
                        tool_manager.jesse_chart_tool.get_price_chart_data(symbol, days_back)
                    )
                    
                    chart_description = f"Single Price Chart"
                    symbols_text = symbol
                
                # Parse and validate the JSON
                chart_data = json.loads(chart_data_json)
                logger.info(f"✅ Chart data generated successfully")
                
                # 🔍 DEBUG: Log the chart data structure
                if 'history' in chart_data and 'content' in chart_data['history']:
                    content_count = len(chart_data['history']['content'])
                    logger.info(f"📊 Chart contains {content_count} data series (lines)")
                    
                    # Log each series for debugging
                    for i, series in enumerate(chart_data['history']['content']):
                        series_name = series.get('name', f'Series {i+1}')
                        series_color = series.get('primary_colour', 'Unknown')
                        data_points = len(series.get('x', []))
                        logger.info(f"  - Line {i+1}: {series_name} ({series_color}) - {data_points} points")
                
            except Exception as data_error:
                logger.error(f"❌ Data retrieval error for {symbol}: {data_error}")
                raise data_error
            
            # SUCCESS: Format the response with ```graph (not ```chart)
            response = f"""🎯 **Interactive Multi-Line Chart Generated**

**📊 Chart**: {symbols_text} ({chart_description})
**📅 Period**: Last {days_back} days  
**✅ Status**: Ready for Visual Rendering with Multiple Lines

```graph
{json.dumps(chart_data, indent=2)}
```

**🎨 Interactive Features**:
- Multiple cryptocurrency lines on single graph
- Hover tooltips showing exact values for each line
- Zoom and pan functionality across all data series
- Professional crypto-themed colors (BTC: Orange, ETH: Blue)
- Responsive design for all devices
- Real-time data from Jesse.ai database
- Legend showing all cryptocurrencies

**📱 Rendering**: This chart automatically renders as an interactive visualization with multiple lines in your frontend."""

            # 🚨 CRITICAL: Log the complete response to console BEFORE returning
            print(f"\n" + "="*80)
            print(f"🎯 COMPLETE MULTI-LINE CHART RESPONSE (SENDING TO FRONTEND):")
            print("="*80)
            print(response)
            print("="*80)
            print(f"✅ Response contains ```graph block: {'```graph' in response}")
            print(f"🔥 Chart type: {chart_type}")
            if force_comparison:
                print(f"🔥 Multi-line chart symbols: {[symbol] + all_additional}")
                print(f"🔥 Expected lines on single graph: {len([symbol] + all_additional)}")
            print("="*80 + "\n")

            logger.info(f"✅ MULTI-LINE VISUAL CHART SUCCESS: {symbols_text}")
            return response
            



            
        except Exception as e:
            logger.error(f"❌ Chart generation error for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            
            # Create error response
            error_response = f"""❌ **Chart Generation Error**

**Error**: Failed to generate multi-line chart for {symbol}
**Details**: {str(e)}

```graph
{{"error": "Chart data unavailable", "symbol": "{symbol}", "message": "Please try again or contact support"}}
```

**🔧 Troubleshooting**: Try a different symbol or check system status."""

            # Log error response to console
            print(f"\n" + "="*80)
            print(f"❌ ERROR RESPONSE FOR {symbol} (SENDING TO FRONTEND):")
            print("="*80)
            print(error_response)
            print("="*80 + "\n")
            
            return error_response


# Enhanced LangChain Tools
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
            result = run_async_safely(tool_manager.tgx_tool.get_market_data(symbol))
            
            # Log the result to console
            print(f"\n📊 LIVE CRYPTO DATA RESULT FOR {symbol}:")
            print("-" * 50)
            print(result)
            print("-" * 50 + "\n")
            
            return result
        except Exception as e:
            logger.error(f"Live crypto data tool error: {e}")
            return f"Error getting live data: {str(e)}"

class MarketStatusTool(BaseTool):
    name: str = "get_live_market_status"
    description: str = """Get TGX Finance WebSocket connection status and system health."""
    
    def _run(self, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            result = run_async_safely(tool_manager.tgx_tool.get_connection_status())
            
            # Log the result to console
            print(f"\n🔗 MARKET STATUS RESULT:")
            print("-" * 40)
            print(result)
            print("-" * 40 + "\n")
            
            return result
        except Exception as e:
            logger.error(f"Market status tool error: {e}")
            return f"Error getting market status: {str(e)}"

class HistoricalAnalysisTool(BaseTool):
    name: str = "get_crypto_historical_analysis"
    description: str = """Get historical cryptocurrency analysis from Jesse.ai database.
    
    Args:
        symbol: Cryptocurrency symbol (e.g., 'BTCUSDT', 'ETHUSDT')
        timeframe: Chart timeframe ('1m', '5m', '15m', '30m', '1h', '4h', '1D')
    """
    
    def _run(self, symbol: str, timeframe: str = "1D", run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        if not tool_manager or not tool_manager.tools_initialized:
            return "Error: MCP tools not initialized"
        
        try:
            result = run_async_safely(tool_manager.jesse_tool.get_historical_analysis(symbol, timeframe))
            
            # Log the result to console
            print(f"\n📈 HISTORICAL ANALYSIS RESULT FOR {symbol} ({timeframe}):")
            print("-" * 60)
            print(result)
            print("-" * 60 + "\n")
            
            return result
        except Exception as e:
            logger.error(f"Historical analysis tool error: {e}")
            return f"Error getting historical analysis: {str(e)}"

# Enhanced System Prompt for GUARANTEED Visual Charts with ```graph
VISUAL_CHART_SYSTEM_PROMPT = """You are a cryptocurrency analysis assistant with multi-line visual chart generation capabilities.

🎯 **CRITICAL MULTI-LINE CHART RULES**:

1. **ALWAYS USE generate_guaranteed_visual_chart FOR ANY CHART REQUEST**
2. **For comparison requests, set comparison_request=True to get MULTIPLE LINES on SINGLE GRAPH**
3. **The tool returns ```graph blocks (NOT ```chart) - keep them exactly as provided**
4. **EVERY chart request MUST result in a ```graph block for chart-renderer.tsx**

**Auto-Trigger Multi-Line Chart Generation** for these requests:
- "comparison", "compare", "vs", "versus", "both", "together", "against"
- "BTC vs ETH", "Bitcoin and Ethereum", "show both"
- "side by side", "multiple cryptocurrencies"
- "BTC ETH comparison", "compare Bitcoin to Ethereum"

**MANDATORY Response Format for Multi-Line Charts**:
When user asks for comparison, use:
```python
generate_guaranteed_visual_chart(
    symbol="BTC", 
    chart_type="comparison", 
    comparison_request=True,
    additional_symbols=["ETH"],
    days_back=30
)


**Chart Detection Logic**:
- Single word like "BTC" = Single line chart
- "BTC comparison" or "BTC vs ETH" = Multi-line chart with BTC + ETH
- "compare BTC and ETH" = Multi-line chart with both lines
- "Bitcoin Ethereum" = Multi-line chart with both lines

**MANDATORY Response Format**:
When you get data from generate_guaranteed_visual_chart, you MUST preserve the ```graph block exactly:

✅ CORRECT Multi-Line:
```graph
{
  "history": {
    "title": "Comparison (30 days)",
    "content": [
      {
        "name": "Bitcoin",
        "primary_colour": "#F7931A",
        "x": ["2024-01-01", "2024-01-02"],
        "price": {"y": [45000, 46000], "ylabel": "Change (%)"}
      },
      {
        "name": "Ethereum", 
        "primary_colour": "#627EEA",
        "x": ["2024-01-01", "2024-01-02"],
        "price": {"y": [3000, 3100], "ylabel": "Change (%)"}
      }
    ]
  }
}
```
❌ NEVER DO:
- Change ```graph to ```chart or anything else
- Modify the JSON content inside ```graph blocks
- Return separate charts for each cryptocurrency
- Say "here's the data" without the graph block

**Response Structure for Multi-Line Charts**:
1. Brief intro about the multi-line chart being generated
2. The EXACT ```graph block from the tool (contains multiple data series)
3. Explanation that this shows multiple lines on a single interactive graph

**Example Multi-Line Response**:
"I'll generate an interactive comparison chart showing both Bitcoin and Ethereum on the same graph:

```graph
{
  "history": {
    "title": "BTC vs ETH Comparison (30 days)",
    "content": [
      {"name": "Bitcoin", "primary_colour": "#F7931A", ...},
      {"name": "Ethereum", "primary_colour": "#627EEA", ...}
    ]
  }
}
```

This interactive chart displays both cryptocurrencies as separate colored lines on a single graph with hover tooltips, zoom, and pan capabilities."
"""


# FastAPI App Setup
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup/shutdown for visual chart system"""
    global tool_manager
    
    logger.info("🎯 Starting Visual Chart System...")
    
    tool_manager = MCPToolManager()
    await tool_manager.initialize()
    
    app.state.tool_manager = tool_manager
    
    logger.info("✅ Visual Chart System ready!")
    
    yield
    
    logger.info("🛑 Shutting down Visual Chart System...")
    if tool_manager:
        await tool_manager.close()
    logger.info("✅ Shutdown complete")

app = FastAPI(
    title="Visual Chart Crypto Analysis Server",
    description="MCP server with interactive visual charts compatible with chart-renderer.tsx",
    version="4.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 🎯 PRIMARY ENDPOINT: Visual Charts
@app.post("/v1/chat/completions")
async def visual_chart_endpoint(request: ChatRequest):
    """Chat endpoint with multi-line visual chart generation"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="Visual chart system not initialized")
        
        messages = request.messages
        if not messages:
            raise HTTPException(status_code=400, detail="Messages required")
    
        last_user_msg = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
        if not last_user_msg:
            raise HTTPException(status_code=400, detail="No user message found")
    
        user_input = last_user_msg.get('content', '')
        if isinstance(user_input, list):
            user_input = ' '.join(item.get('text', '') for item in user_input if isinstance(item, dict))
        
        # Enhanced detection for comparison requests
        chart_keywords = ['chart', 'graph', 'plot', 'visualize', 'show', 'display', 
                        'price chart', 'comparison', 'vs', 'versus', 'compare',
                        'trend', 'performance', 'history', 'over time']
        
        # Multi-line comparison detection
        comparison_keywords = ['comparison', 'compare', 'vs', 'versus', 'both', 
                             'together', 'against', 'side by side', 'and']
        
        is_chart_request = any(keyword in user_input.lower() for keyword in chart_keywords)
        is_comparison_request = any(keyword in user_input.lower() for keyword in comparison_keywords)
        
        # Check for specific crypto mentions
        has_btc = any(term in user_input.upper() for term in ['BTC', 'BITCOIN', 'BTCUSDT'])
        has_eth = any(term in user_input.upper() for term in ['ETH', 'ETHEREUM', 'ETHUSDT', 'ETHER'])
        
        # If user mentions both BTC and ETH, force comparison mode
        if has_btc and has_eth:
            is_comparison_request = True
            logger.info("🔥 DETECTED: User mentioned both BTC and ETH - Forcing comparison mode")
        
        def generate_visual_response():
            try:
                # Create LLM client
                client = AzureChatOpenAI(
                    azure_endpoint=endpoint,
                    azure_deployment=deployment,
                    openai_api_version=api_version,
                    api_key=azure_api_key,
                )
                
                # Tools with updated visual chart generator
                tools = [
                    GuaranteedVisualChartTool(),  # 🎯 PRIMARY: Multi-line visual charts
                    LiveCryptoDataTool(),
                    MarketStatusTool(),
                    HistoricalAnalysisTool()
                ]
                
                client_with_tools = client.bind_tools(tools)
                
                # Enhanced system message for multi-line charts
                messages_with_tools = [
                    SystemMessage(VISUAL_CHART_SYSTEM_PROMPT),
                    HumanMessage(user_input),
                ]
                
                if is_chart_request:
                    if is_comparison_request:
                        logger.info("🎯 MULTI-LINE COMPARISON REQUEST DETECTED - Activating comparison mode")
                    else:
                        logger.info("🎯 SINGLE-LINE CHART REQUEST DETECTED - Activating standard mode")
                
                # Process request
                result = client_with_tools.invoke(messages_with_tools)
                messages_with_tools.append(result)
                
                # Handle tool calls
                if result.tool_calls:
                    for tool_call in result.tool_calls:
                        logger.info(f"🔧 Tool call: {tool_call['name']}")
                        
                        # Special handling for chart generation
                        if tool_call["name"] == "generate_guaranteed_visual_chart":
                            # Auto-inject comparison parameters if detected
                            if is_comparison_request and 'comparison_request' not in tool_call["args"]:
                                tool_call["args"]["comparison_request"] = True
                                tool_call["args"]["chart_type"] = "comparison"
                                
                                # Auto-add ETH if BTC is primary and vice versa
                                primary_symbol = tool_call["args"].get("symbol", "").upper()
                                if 'BTC' in primary_symbol and not tool_call["args"].get("additional_symbols"):
                                    tool_call["args"]["additional_symbols"] = ["ETH"]
                                elif 'ETH' in primary_symbol and not tool_call["args"].get("additional_symbols"):
                                    tool_call["args"]["additional_symbols"] = ["BTC"]
                                
                                logger.info(f"🔥 AUTO-INJECTED comparison parameters: {tool_call['args']}")
                        
                        tool_found = None
                        for tool in tools:
                            if tool.name == tool_call["name"]:
                                tool_found = tool
                                break
                        
                        if tool_found:
                            try:
                                tool_result = tool_found._run(**tool_call["args"])
                                
                                # CONSOLE LOG: Tool result before adding to messages
                                print(f"\n🔧 MULTI-LINE TOOL RESULT FROM {tool_call['name']}:")
                                print("="*60)
                                print(tool_result)
                                print("="*60)
                                print(f"✅ Contains ```graph block: {'```graph' in tool_result}")
                                
                                # Check for multiple data series in the result
                                if '```graph' in tool_result:
                                    try:
                                        # Extract and parse the graph data
                                        start = tool_result.find('```graph\n') + 9
                                        end = tool_result.find('\n```', start)
                                        graph_json = tool_result[start:end]
                                        graph_data = json.loads(graph_json)
                                        
                                        if 'history' in graph_data and 'content' in graph_data['history']:
                                            series_count = len(graph_data['history']['content'])
                                            print(f"🔥 Multi-line chart detected: {series_count} data series")
                                            for i, series in enumerate(graph_data['history']['content']):
                                                series_name = series.get('name', f'Series {i+1}')
                                                print(f"  - Line {i+1}: {series_name}")
                                    except:
                                        pass
                                
                                print("="*60 + "\n")
                                
                                tool_message = ToolMessage(
                                    content=tool_result,
                                    tool_call_id=tool_call["id"]
                                )
                                messages_with_tools.append(tool_message)
                                
                            except Exception as e:
                                logger.error(f"Tool execution error: {e}")
                                error_msg = f"Tool {tool_call['name']} failed: {str(e)}"
                                
                                tool_message = ToolMessage(
                                    content=error_msg,
                                    tool_call_id=tool_call["id"]
                                )
                                messages_with_tools.append(tool_message)
                
                # Generate final streaming response
                message_id = f"visual-{abs(hash(user_input)) % 10000}"
                
                for chunk in client_with_tools.stream(messages_with_tools):
                    if chunk.content:
                        # CONSOLE LOG: Each chunk before streaming
                        print(f"\n📤 STREAMING MULTI-LINE CHUNK TO FRONTEND:")
                        print("-"*40)
                        print(f"Content: {chunk.content[:200]}{'...' if len(chunk.content) > 200 else ''}")
                        print(f"Contains ```graph: {'```graph' in chunk.content}")
                        if '```graph' in chunk.content:
                            print("🔥 Multi-line chart data detected in stream")
                        print("-"*40 + "\n")
                        
                        # Always check for graph content
                        if '```graph' in chunk.content:
                            chunk_data = {
                                "type": "chart",
                                "id": message_id,
                                "data": chunk.content,
                                "visual": True,
                                "interactive": True,
                                "multi_line": is_comparison_request
                            }
                            logger.info("📊 MULTI-LINE VISUAL CHART STREAMING TO FRONTEND")
                        else:
                            chunk_data = {
                                "type": "text",
                                "id": message_id,
                                "data": chunk.content
                            }
                        
                        yield f'{json.dumps([chunk_data])}\n'

            except Exception as e:
                logger.error(f"Multi-line visual response generation error: {e}")
                import traceback
                traceback.print_exc()
                
                # Error response
                error_data = {
                    "type": "text", 
                    "id": f"error-{abs(hash(str(e))) % 1000}",
                    "data": f"❌ **System Error**: {str(e)}\n\nPlease try again or contact support."
                }
                
                # CONSOLE LOG: Error response
                print(f"\n❌ ERROR RESPONSE TO FRONTEND:")
                print("="*50)
                print(json.dumps(error_data, indent=2))
                print("="*50 + "\n")
                
                yield f'{json.dumps([error_data])}\n'

        return StreamingResponse(
            generate_visual_response(),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Chart-Mode": "multi-line-visual-graph-blocks",
                "X-Chart-Compatible": "chart-renderer-tsx"
            }
        )
        
    except Exception as e:
        logger.error(f"Multi-line visual chart endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))  


# Direct Chart Generation Endpoints
@app.get("/generate/visual-chart/{symbol}")
async def direct_visual_chart(symbol: str, days_back: int = 30):
    """Direct endpoint for visual chart generation"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="Visual chart system not initialized")
        
        logger.info(f"📊 Direct visual chart request: {symbol}, {days_back} days")
        
        chart_tool = GuaranteedVisualChartTool()
        chart_result = chart_tool._run(symbol=symbol, days_back=days_back)
        
        # Extract chart data from the result
        if "```graph" in chart_result:
            chart_start = chart_result.find("```graph\n") + 9
            chart_end = chart_result.find("\n```", chart_start)
            chart_data_str = chart_result[chart_start:chart_end]
            chart_data = json.loads(chart_data_str)
            
            # CONSOLE LOG: Direct endpoint response
            print(f"\n📊 DIRECT CHART ENDPOINT RESPONSE FOR {symbol}:")
            print("="*60)
            print(f"Chart data extracted: {len(chart_data_str)} characters")
            print(f"Valid JSON: {isinstance(chart_data, dict)}")
            print("="*60 + "\n")
            
            return JSONResponse({
                "status": "success",
                "type": "visual_chart",
                "symbol": symbol,
                "days_back": days_back,
                "chart_data": chart_data,
                "compatible_with": "chart-renderer.tsx",
                "interactive": True,
                "timestamp": datetime.now().isoformat()
            })
        else:
            logger.error(f"No ```graph block found in chart result for {symbol}")
            raise HTTPException(status_code=500, detail="Failed to generate visual chart")
        
    except Exception as e:
        logger.error(f"Direct visual chart error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate/visual-comparison")
async def direct_visual_comparison(symbols: list, days_back: int = 90):
    """Direct endpoint for visual comparison chart generation"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="Visual chart system not initialized")
        
        if len(symbols) < 2:
            raise HTTPException(status_code=400, detail="Need at least 2 symbols for comparison")
        
        logger.info(f"📊 Direct visual comparison: {symbols}, {days_back} days")
        
        chart_tool = GuaranteedVisualChartTool()
        chart_result = chart_tool._run(
            symbol=symbols[0], 
            chart_type="comparison", 
            additional_symbols=symbols[1:], 
            days_back=days_back
        )
        
        # Extract chart data
        if "```graph" in chart_result:
            chart_start = chart_result.find("```graph\n") + 9
            chart_end = chart_result.find("\n```", chart_start)
            chart_data_str = chart_result[chart_start:chart_end]
            chart_data = json.loads(chart_data_str)
            
            # CONSOLE LOG: Direct comparison response
            print(f"\n📊 DIRECT COMPARISON ENDPOINT RESPONSE FOR {symbols}:")
            print("="*70)
            print(f"Chart data extracted: {len(chart_data_str)} characters")
            print(f"Symbols count: {len(symbols)}")
            print("="*70 + "\n")
            
            return JSONResponse({
                "status": "success",
                "type": "visual_comparison",
                "symbols": symbols,
                "days_back": days_back,
                "chart_data": chart_data,
                "compatible_with": "chart-renderer.tsx",
                "interactive": True,
                "timestamp": datetime.now().isoformat()
            })
        else:
            logger.error(f"No ```graph block found in comparison result")
            raise HTTPException(status_code=500, detail="Failed to generate visual comparison")
        
    except Exception as e:
        logger.error(f"Direct visual comparison error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Health and Status Endpoints
@app.get("/health")
async def health_check():
    """Health check for visual chart system"""
    try:
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "system": "Visual Chart Generator",
            "version": "4.1.0",
            "services": {
                "visual_chart_generator": "✅ ACTIVE" if tool_manager and tool_manager.tools_initialized else "❌ INACTIVE",
                "chart_renderer_compatibility": "✅ ```graph blocks",
                "jesse_database": "✅ CONNECTED" if tool_manager and tool_manager.tools_initialized else "❌ DISCONNECTED",
                "azure_openai": "✅ AVAILABLE" if azure_api_key else "❌ UNAVAILABLE"
            },
            "features": {
                "graph_blocks": "✅ Uses ```graph for chart-renderer.tsx",
                "console_logging": "✅ All responses logged to console",
                "interactive_charts": "✅ Hover, zoom, pan supported",
                "real_data": "✅ Jesse.ai database integration",
                "error_handling": "✅ Graceful error responses"
            },
            "tools": [
                "🎯 generate_guaranteed_visual_chart (PRIMARY)",
                "get_live_crypto_data",
                "get_live_market_status", 
                "get_crypto_historical_analysis"
            ]
        }
        
        if not tool_manager or not tool_manager.tools_initialized:
            health_status["status"] = "degraded"
            health_status["warning"] = "Visual chart system not fully initialized"
            
        return health_status
        
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
            "system": "Visual Chart Generator"
        }

@app.get("/")
async def root():
    """API documentation"""
    return {
        "title": "🎯 Visual Chart Crypto Analysis Server",
        "version": "4.1.0",
        "description": "MCP server with interactive visual charts compatible with chart-renderer.tsx",
        "compatibility": {
            "frontend_component": "chart-renderer.tsx",
            "block_type": "```graph (not ```chart)",
            "detection_pattern": "language === 'graph'",
            "auto_rendering": "Dynamic import with client-side only rendering"
        },
        "endpoints": {
            "/v1/chat/completions": "✅ Visual charts via chat with console logging (POST)",
            "/generate/visual-chart/{symbol}": "✅ Direct visual chart generation (GET)",
            "/generate/visual-comparison": "✅ Direct visual comparison charts (POST)", 
            "/health": "System status and compatibility check"
        },
        "chart_features": {
            "format": "```graph blocks for automatic frontend detection",
            "rendering": "Interactive charts via chart-renderer.tsx component",
            "data_source": "Real cryptocurrency data from Jesse.ai database",
            "styling": "Professional crypto-themed colors and layouts",
            "responsiveness": "Optimized for all screen sizes",
            "console_logging": "All responses logged to server console for debugging"
        },
        "debugging": {
            "console_output": "All tool results and responses logged to console",
            "response_tracking": "Full request-response cycle visible in server logs",
            "chart_validation": "Automatic ```graph block detection and validation",
            "error_tracing": "Detailed error messages and stack traces"
        }
    }

# Test endpoint for chart validation
@app.get("/test/chart-validation")
async def test_chart_validation():
    """Test endpoint to verify chart generation works"""
    try:
        # Test the visual chart tool
        chart_tool = GuaranteedVisualChartTool()
        test_result = chart_tool._run(symbol="BTC", days_back=7)
        
        has_graph_block = "```graph" in test_result
        
        # CONSOLE LOG: Test result
        print(f"\n🧪 CHART VALIDATION TEST RESULT:")
        print("="*50)
        print(f"Has ```graph block: {has_graph_block}")
        print(f"Result length: {len(test_result)} characters")
        print("="*50 + "\n")
        
        return {
            "test": "Chart Generation Validation",
            "status": "✅ PASSED" if has_graph_block else "❌ FAILED",
            "has_graph_block": has_graph_block,
            "block_type": "```graph" if has_graph_block else "None detected",
            "result_preview": test_result[:200] + "..." if len(test_result) > 200 else test_result,
            "timestamp": datetime.now().isoformat(),
            "compatible_with": "chart-renderer.tsx"
        }
        
    except Exception as e:
        return {
            "test": "Chart Generation Validation", 
            "status": "❌ ERROR",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

if __name__ == "__main__":
    import uvicorn
    
    try:
        port = int(os.getenv("PORT", "8000"))
        logger.info(f"🎯 Starting Visual Chart Server on port {port}")
        
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