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

# Additional Tool Classes (that were missing)
class LiveCryptoDataTool(BaseTool):
    name: str = "get_live_crypto_data"
    description: str = "Get current live cryptocurrency prices and market data"
    
    def _run(self, symbol: str = "BTC", run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        try:
            # Use the TGX tool to get live data
            if tool_manager and tool_manager.tools_initialized:
                live_data = run_async_safely(
                    tool_manager.tgx_tool.get_market_data(symbol)
                )
                return f"Live {symbol} data: {live_data}"
            else:
                return f"Live data tool not initialized for {symbol}"
        except Exception as e:
            return f"Error getting live data for {symbol}: {str(e)}"

class MarketStatusTool(BaseTool):
    name: str = "get_market_status"
    description: str = "Get current cryptocurrency market status and overview"
    
    def _run(self, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        try:
            # Get market status from available tools
            if tool_manager and tool_manager.tools_initialized:
                # Use TGX tool to get market overview
                market_data = run_async_safely(
                    tool_manager.tgx_tool.get_market_data("BTC")
                )
                return f"Market status: {market_data}"
            else:
                return "Market status tool not initialized"
        except Exception as e:
            return f"Error getting market status: {str(e)}"

class HistoricalAnalysisTool(BaseTool):
    name: str = "get_historical_analysis"
    description: str = "Get historical analysis and trends for cryptocurrencies"
    
    def _run(self, symbol: str = "BTC", days: int = 30, run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        try:
            # Use Jesse tool for historical analysis
            if tool_manager and tool_manager.tools_initialized:
                historical_data = run_async_safely(
                    tool_manager.jesse_tool.get_historical_data(symbol, days)
                )
                return f"Historical analysis for {symbol} ({days} days): {historical_data}"
            else:
                return f"Historical analysis tool not initialized for {symbol}"
        except Exception as e:
            return f"Error getting historical analysis for {symbol}: {str(e)}"

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
        import concurrent.futures
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
        timeframe: Chart interval ('1m', '5m', '15m', '30m', '1h', '4h', '1D') - default '1D'
        chart_type: 'price' for single crypto, 'comparison' for BTC+ETH multi-line chart
        additional_symbols: List of symbols for comparison (ignored - always uses BTC+ETH)
        comparison_request: Set to True to force BTC+ETH comparison mode
    
    Returns:
        Interactive visual chart in ```graph blocks with GUARANTEED multi-line support
    """
    
    def _run(self, symbol: str, days_back: int = 30, timeframe: str = "1D",
             chart_type: str = "price", additional_symbols: list = None, 
             comparison_request: bool = False,
             run_manager: Optional[CallbackManagerForToolRun] = None) -> str:
        
        try:
            logger.info(f"🎯 GENERATING VISUAL CHART: {symbol}, {days_back}d, {timeframe}, type: {chart_type}, comparison: {comparison_request}")
            
            # Ensure tools are initialized
            if not tool_manager or not tool_manager.tools_initialized:
                error_msg = "Chart tools not initialized"
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)
            
            # Validate timeframe
            valid_timeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1D', '1d']
            if timeframe not in valid_timeframes:
                logger.warning(f"Invalid timeframe {timeframe}, defaulting to 1D")
                timeframe = '1D'
            
            # 🔥 CRITICAL: Force BTC+ETH comparison when comparison is requested
            if comparison_request or chart_type == "comparison":
                logger.info("🔥 COMPARISON REQUEST DETECTED - Generating GUARANTEED BTC vs ETH multi-line chart")
                return self._generate_guaranteed_btc_eth_comparison(days_back, timeframe)
            
            # Check for comparison keywords in symbol or implicit comparison request
            symbol_upper = symbol.upper()
            comparison_indicators = ['VS', 'VERSUS', 'COMPARE', 'COMPARISON']
            
            # Auto-detect comparison requests
            if (any(indicator in symbol_upper for indicator in comparison_indicators) or 
                ('BTC' in symbol_upper and 'ETH' in symbol_upper)):
                logger.info("🔥 AUTO-DETECTED COMPARISON REQUEST - Generating GUARANTEED BTC vs ETH multi-line chart")
                return self._generate_guaranteed_btc_eth_comparison(days_back, timeframe)
            
            # Generate single price chart
            try:
                logger.info(f"📊 Generating SINGLE PRICE CHART: {symbol} ({timeframe})")
                
                chart_data_json = run_async_safely(
                    tool_manager.jesse_chart_tool.get_price_chart_data(symbol, days_back, timeframe)
                )
                
            except Exception as data_error:
                logger.error(f"❌ Data retrieval error for {symbol}: {data_error}")
                raise data_error
            
            # Parse and validate the JSON
            chart_data = json.loads(chart_data_json)
            logger.info(f"✅ Single chart data generated successfully")
            
            # Get timeframe display name for response
            timeframe_labels = {
                '1m': '1-Minute',
                '5m': '5-Minute', 
                '15m': '15-Minute',
                '30m': '30-Minute', 
                '1h': 'Hourly',
                '4h': '4-Hour',
                '1D': 'Daily',
                '1d': 'Daily'
            }
            timeframe_display = timeframe_labels.get(timeframe, timeframe)
            
            # SUCCESS: Format the response with ```graph
            response = f"""🎯 **Interactive Price Chart Generated**

**📊 Chart**: {symbol} Price Chart
**📅 Period**: Last {days_back} days  
**⏱️ Timeframe**: {timeframe_display}
**✅ Status**: Ready for Visual Rendering

```graph
{json.dumps(chart_data, indent=2)}
```

**🎨 Interactive Features**:
- {timeframe_display} interval data resolution
- Hover tooltips showing exact values
- Zoom and pan functionality
- Professional crypto-themed colors
- Responsive design for all devices
- Real-time data from Jesse.ai database

**📱 Rendering**: This chart automatically renders as an interactive visualization in your frontend."""

            logger.info(f"✅ SINGLE PRICE CHART SUCCESS: {symbol}")
            return response
            
        except Exception as e:
            logger.error(f"❌ Chart generation error for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            
            # Create error response
            error_response = f"""❌ **Chart Generation Error**

**Error**: Failed to generate chart for {symbol}
**Timeframe**: {timeframe}
**Details**: {str(e)}

```graph
{{"error": "Chart data unavailable", "symbol": "{symbol}", "timeframe": "{timeframe}", "message": "Please try again or contact support"}}
```

**🔧 Troubleshooting**: Try a different symbol, timeframe, or check system status."""

            return error_response

    def _generate_guaranteed_btc_eth_comparison(self, days_back: int = 30, timeframe: str = "1D") -> str:
        """🔥 GUARANTEED: Generate BTC vs ETH comparison chart with MULTIPLE LINES and proper timeframe"""
        try:
            logger.info(f"🎯 GENERATING GUARANTEED BTC vs ETH MULTI-LINE COMPARISON ({timeframe})")
            
            # Ensure tools are initialized
            if not tool_manager or not tool_manager.tools_initialized:
                error_msg = "Chart tools not initialized for comparison"
                logger.error(f"❌ {error_msg}")
                raise Exception(error_msg)
            
            # 🔥 FORCE BTC and ETH symbols - NO EXCEPTIONS
            symbols = ['BTC', 'ETH']
            
            logger.info(f"📊 Calling get_comparison_chart_data for GUARANTEED BTC vs ETH with timeframe {timeframe}...")
            chart_data_json = run_async_safely(
                tool_manager.jesse_chart_tool.get_comparison_chart_data(symbols, days_back, timeframe)
            )
            
            # Parse and validate the JSON
            chart_data = json.loads(chart_data_json)
            logger.info(f"✅ BTC vs ETH comparison chart data parsed successfully")
            
            # 🔍 CRITICAL VALIDATION: Verify multi-line structure
            if 'history' in chart_data and 'content' in chart_data['history']:
                content_count = len(chart_data['history']['content'])
                logger.info(f"📊 Comparison chart contains {content_count} data series (lines)")
                
                if content_count < 2:
                    logger.error(f"❌ CRITICAL ERROR: Expected 2 lines (BTC+ETH) but got {content_count}")
                    raise Exception(f"FAILED: Multi-line comparison chart only created {content_count} series instead of 2")
                
                # Verify we have Bitcoin and Ethereum specifically
                series_names = [series.get('name', 'Unknown') for series in chart_data['history']['content']]
                logger.info(f"📊 Series names detected: {series_names}")
                
                has_bitcoin = any('Bitcoin' in name for name in series_names)
                has_ethereum = any('Ethereum' in name for name in series_names)
                
                if not (has_bitcoin and has_ethereum):
                    logger.error(f"❌ Missing BTC or ETH in series: {series_names}")
                    raise Exception(f"FAILED: Expected Bitcoin and Ethereum series, got: {series_names}")
                
                # Log each series for debugging
                for i, series in enumerate(chart_data['history']['content']):
                    series_name = series.get('name', f'Series {i+1}')
                    series_color = series.get('primary_colour', 'Unknown')
                    data_points = len(series.get('x', []))
                    sample_prices = series.get('price', {}).get('y', [])[:3]
                    logger.info(f"  - Line {i+1}: {series_name} ({series_color}) - {data_points} points")
                    logger.info(f"    Sample prices: {sample_prices}")
                    
            else:
                logger.error("❌ Invalid chart data structure - missing history.content")
                raise Exception("Invalid chart data structure for comparison chart")
            
            # Get timeframe display name
            timeframe_labels = {
                '1m': '1-Minute',
                '5m': '5-Minute', 
                '15m': '15-Minute',
                '30m': '30-Minute', 
                '1h': 'Hourly',
                '4h': '4-Hour',
                '1D': 'Daily',
                '1d': 'Daily'
            }
            timeframe_display = timeframe_labels.get(timeframe, timeframe)
            
            # SUCCESS: Format the response with ```graph
            response = f"""🎯 **Interactive BTC vs ETH Multi-Line Comparison Chart Generated**

**📊 Chart**: Bitcoin vs Ethereum Multi-Line Comparison  
**📅 Period**: Last {days_back} days
**⏱️ Timeframe**: {timeframe_display}
**✅ Status**: Ready for Visual Rendering with {len(chart_data['history']['content'])} SEPARATE Lines

```graph
{json.dumps(chart_data, indent=2)}
```

**🎨 Interactive Features**:
- **{len(chart_data['history']['content'])} SEPARATE cryptocurrency lines** on single graph (Bitcoin: Orange, Ethereum: Blue)
- {timeframe_display} interval data resolution
- Hover tooltips showing exact values for EACH line
- Zoom and pan functionality across all data series
- Professional crypto-themed colors
- Responsive design for all devices
- Real-time data from Jesse.ai database
- Legend showing BOTH cryptocurrencies clearly

**📱 Rendering**: This comparison chart automatically renders as an interactive visualization with **{len(chart_data['history']['content'])} DISTINCT COLORED LINES** in your frontend."""

            # 🚨 CRITICAL: Log the complete response to console BEFORE returning
            print(f"\n" + "="*90)
            print(f"🔥 GUARANTEED BTC vs ETH MULTI-LINE COMPARISON CHART RESPONSE (SENDING TO FRONTEND):")
            print("="*90)
            print(f"TIMEFRAME: {timeframe} ({timeframe_display})")
            print(f"LINES IN CHART: {len(chart_data['history']['content'])}")
            print(f"SERIES NAMES: {[s.get('name') for s in chart_data['history']['content']]}")
            print(f"SERIES COLORS: {[s.get('primary_colour') for s in chart_data['history']['content']]}")
            print(f"TITLE: {chart_data['history'].get('title', 'No title')}")
            print(f"RESPONSE LENGTH: {len(response)} characters")
            print(f"CONTAINS ```graph: {'```graph' in response}")
            print("-"*90)
            print("CHART DATA PREVIEW:")
            print(json.dumps(chart_data, indent=2)[:800] + "..." if len(json.dumps(chart_data)) > 800 else json.dumps(chart_data, indent=2))
            print("="*90)
            
            # Final validation
            if len(chart_data['history']['content']) != 2:
                print("⚠️ WARNING: EXPECTED EXACTLY 2 LINES (BTC+ETH) - COMPARISON MAY NOT WORK PROPERLY")
            else:
                print("✅ SUCCESS: 2 LINES CONFIRMED (BTC+ETH) - GUARANTEED MULTI-LINE COMPARISON READY")
            
            print("="*90 + "\n")

            logger.info(f"✅ GUARANTEED BTC vs ETH MULTI-LINE COMPARISON CHART SUCCESS")
            return response
            
        except Exception as e:
            logger.error(f"❌ GUARANTEED BTC vs ETH multi-line comparison chart generation error: {e}")
            import traceback
            traceback.print_exc()
            
            # Create error response
            error_response = f"""❌ **BTC vs ETH Multi-Line Comparison Chart Generation Error**

**Error**: Failed to generate GUARANTEED BTC vs ETH multi-line comparison chart
**Timeframe**: {timeframe}
**Details**: {str(e)}

```graph
{{"error": "BTC vs ETH multi-line comparison chart data unavailable", "timeframe": "{timeframe}", "message": "System error - please try again or contact support"}}
```

**🔧 Troubleshooting**: Try different timeframe or check system status."""

            # Log error response to console
            print(f"\n" + "="*80)
            print(f"❌ BTC vs ETH MULTI-LINE COMPARISON ERROR RESPONSE (SENDING TO FRONTEND):")
            print("="*80)
            print(error_response)
            print("="*80 + "\n")
            
            return error_response

# Updated system prompt for multi-line charts
VISUAL_CHART_SYSTEM_PROMPT = """You are a cryptocurrency analysis assistant with GUARANTEED multi-line visual chart generation and timeframe support.

🎯 **CRITICAL MULTI-LINE CHART RULES**:

1. **ALWAYS USE generate_guaranteed_visual_chart FOR ANY CHART REQUEST**
2. **For ANY comparison request, ALWAYS generate BTC vs ETH chart with comparison_request=True**
3. **ALWAYS specify timeframe parameter and ensure title reflects the EXACT timeframe**
4. **The tool returns ```graph blocks (NOT ```chart) - keep them exactly as provided**
5. **EVERY chart request MUST result in a ```graph block for chart-renderer.tsx**
6. **Multi-line charts are GUARANTEED to show 2 separate lines for BTC and ETH**

**🔥 GUARANTEED COMPARISON DETECTION - ALWAYS TRIGGER BTC+ETH MULTI-LINE COMPARISON FOR**:
- ANY mention of "comparison", "compare", "vs", "versus", "both", "together", "against"
- "BTC vs ETH", "Bitcoin and Ethereum", "show both", "side by side", "two lines"
- "multiple cryptocurrencies", "two cryptocurrencies", "crypto comparison"  
- User asks for "BTC and ETH", "Bitcoin versus Ethereum", "compare them"
- "difference between", "which is better", "performance comparison"
- "multi-line", "separate lines", "different colors"

**🕐 CRITICAL TIMEFRAME DETECTION AND TITLE REQUIREMENTS**:
- "hourly", "1 hour", "hour", "1h" → timeframe="1h", title MUST include "(Hourly)"
- "4 hour", "4h", "4-hour" → timeframe="4h", title MUST include "(4-Hour)"  
- "15 minute", "15min", "15m" → timeframe="15m", title MUST include "(15-Minute)"
- "30 minute", "30min", "30m" → timeframe="30m", title MUST include "(30-Minute)"
- "5 minute", "5min", "5m" → timeframe="5m", title MUST include "(5-Minute)"
- "1 minute", "1min", "1m" → timeframe="1m", title MUST include "(1-Minute)"
- "daily", "day", "1d", "1D" → timeframe="1D", title MUST include "(Daily)" - DEFAULT
- "weekly", "week" → timeframe="1D", title MUST include "(Weekly View - Daily Data)"

**🔥 MANDATORY Response Format for GUARANTEED BTC+ETH Multi-Line Comparison**:
When user asks for ANY comparison, ALWAYS use:
```python
generate_guaranteed_visual_chart(
    symbol="BTC",  # Primary symbol (ignored for comparison)
    chart_type="comparison", 
    comparison_request=True,  # THIS FORCES GUARANTEED BTC+ETH MULTI-LINE
    days_back=30,
    timeframe="1D"  # ALWAYS specify detected timeframe
)
```

**📊 Chart Detection Logic with GUARANTEED BTC+ETH Multi-Line and Proper Titles**:
- "BTC hourly" = Single BTC chart with timeframe="1h", title: "Bitcoin Price - Last X Days (Hourly)"
- "compare BTC and ETH" = GUARANTEED BTC+ETH multi-line with timeframe="1D", title: "Bitcoin vs Ethereum Comparison - Last X Days (Daily)"
- "BTC vs ETH 4h" = GUARANTEED BTC+ETH multi-line with timeframe="4h", title: "Bitcoin vs Ethereum Comparison - Last X Days (4-Hour)"
- "show both Bitcoin and Ethereum weekly" = GUARANTEED BTC+ETH multi-line, title: "Bitcoin vs Ethereum Comparison - Last X Days (Weekly View - Daily Data)"
- "Bitcoin vs Ethereum 15 minute chart" = GUARANTEED BTC+ETH multi-line with timeframe="15m", title: "Bitcoin vs Ethereum Comparison - Last X Days (15-Minute)"
- "two lines showing BTC and ETH" = GUARANTEED BTC+ETH multi-line comparison

**🚫 NEVER DO - CRITICAL MISTAKES TO AVOID**:
- Don't create separate charts for each cryptocurrency
- Don't change ```graph to ```chart or anything else
- Don't modify the JSON content inside ```graph blocks
- Don't ignore timeframe specifications in titles  
- Don't use anything other than BTC+ETH for comparisons
- Don't say "here's the data" without the graph block
- Don't create single-line charts when comparison is requested

**✅ MANDATORY Response Format Examples**:

**For Single Crypto with Specific Timeframe:**
```graph
{
  "history": {
    "title": "Bitcoin Price - Last 30 Days (Hourly)",
    "xlabel": "Date",
    "content": [{
      "name": "Bitcoin",
      "primary_colour": "#F7931A",
      "x": ["2024-01-01 00:00:00", "2024-01-01 01:00:00"],
      "price": {"y": [45000, 46000], "ylabel": "Price (USD)"}
    }]
  }
}
```

**For GUARANTEED BTC+ETH Multi-Line Comparison with Proper Timeframe Title:**
```graph
{
  "history": {
    "title": "Bitcoin vs Ethereum Comparison - Last 30 Days (Hourly)",
    "xlabel": "Date", 
    "content": [
      {
        "name": "Bitcoin",
        "primary_colour": "#F7931A",
        "x": ["2024-01-01 00:00:00", "2024-01-01 01:00:00"],
        "price": {"y": [45000, 46000], "ylabel": "Price (USD)"}
      },
      {
        "name": "Ethereum", 
        "primary_colour": "#627EEA",
        "x": ["2024-01-01 00:00:00", "2024-01-01 01:00:00"],
        "price": {"y": [3000, 3100], "ylabel": "Price (USD)"}
      }
    ]
  }
}
```

**Response Structure for GUARANTEED BTC+ETH Multi-Line Comparison Charts**:
1. Brief intro about the GUARANTEED BTC vs ETH multi-line comparison chart being generated
2. The EXACT ```graph block from the tool (contains BOTH BTC and ETH data series as separate content entries)
3. Explanation that this shows BTC (orange) and ETH (blue) lines as TWO SEPARATE LINES on a SINGLE interactive graph

**Example GUARANTEED BTC+ETH Multi-Line Comparison Response with Correct Timeframe**:
"I'll generate an interactive multi-line comparison chart showing Bitcoin and Ethereum as two separate colored lines on the same graph with hourly data:

```graph
{
  "history": {
    "title": "Bitcoin vs Ethereum Comparison - Last 30 Days (Hourly)",
    "xlabel": "Date",
    "content": [
      {"name": "Bitcoin", "primary_colour": "#F7931A", "x": [...], "price": {"y": [...], "ylabel": "Price (USD)"}},
      {"name": "Ethereum", "primary_colour": "#627EEA", "x": [...], "price": {"y": [...], "ylabel": "Price (USD)"}}
    ]
  }
}
```

This interactive multi-line chart displays Bitcoin (orange line) and Ethereum (blue line) as TWO SEPARATE COLORED LINES on a single graph with hourly intervals, including hover tooltips, zoom, and pan capabilities."

🎯 **CRITICAL**: ALWAYS force GUARANTEED BTC+ETH multi-line comparison when ANY comparison is requested, regardless of what cryptocurrencies the user mentions. The system is optimized for Bitcoin vs Ethereum comparisons with GUARANTEED multi-line output showing 2 distinct colored lines.

🔥 **TITLE VERIFICATION**: Every chart title MUST reflect the detected timeframe. Never leave timeframe out of the title.

📊 **MULTI-LINE GUARANTEE**: Every comparison chart MUST contain exactly 2 series (BTC + ETH) in the content array, each with their own name, color, and data arrays.

🚨 **CHART RENDERER COMPATIBILITY**: The format uses history.content array with multiple entries (one per line) - this is the CORRECT format for chart-renderer.tsx to process multiple lines."""

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

# 🎯 FIXED ENDPOINT: Visual Charts with Guaranteed BTC+ETH Comparison
@app.post("/v1/chat/completions")
async def visual_chart_endpoint(request: ChatRequest):
    """Chat endpoint with GUARANTEED BTC+ETH comparison and proper timeframe titles"""
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
        
        # Enhanced detection for comparison requests and timeframes
        chart_keywords = ['chart', 'graph', 'plot', 'visualize', 'show', 'display', 
                        'price chart', 'comparison', 'vs', 'versus', 'compare',
                        'trend', 'performance', 'history', 'over time']
        
        # 🔥 ENHANCED: More comprehensive comparison detection
        comparison_keywords = [
            'comparison', 'compare', 'vs', 'versus', 'both', 'together', 'against',
            'side by side', 'and', 'two', 'multiple', 'crypto comparison',
            'bitcoin and ethereum', 'btc and eth', 'btc vs eth', 'bitcoin vs ethereum',
            'show both', 'compare them', 'between', 'difference between'
        ]
        
        # 🔥 FIXED: Better timeframe detection with weekly support
        timeframe_patterns = {
            '1m': ['1 minute', '1min', '1m', 'minute'],
            '5m': ['5 minute', '5min', '5m', '5 minutes'],
            '15m': ['15 minute', '15min', '15m', '15 minutes'],
            '30m': ['30 minute', '30min', '30m', '30 minutes'],
            '1h': ['1 hour', '1h', 'hour', 'hourly', '1hr'],
            '4h': ['4 hour', '4h', '4 hours', '4hr', '4-hour'],
            '1D': ['daily', 'day', '1d', '1D', '1 day', 'weekly', 'week']  # Weekly uses daily data
        }
        
        detected_timeframe = '1D'  # Default
        is_weekly_request = False
        
        for timeframe, patterns in timeframe_patterns.items():
            if any(pattern.lower() in user_input.lower() for pattern in patterns):
                detected_timeframe = timeframe
                if any(weekly in user_input.lower() for weekly in ['weekly', 'week']):
                    is_weekly_request = True
                logger.info(f"🕐 DETECTED TIMEFRAME: {detected_timeframe}")
                break
        
        is_chart_request = any(keyword in user_input.lower() for keyword in chart_keywords)
        
        # 🔥 ENHANCED: Better comparison detection
        is_comparison_request = any(keyword in user_input.lower() for keyword in comparison_keywords)
        
        # Check for specific crypto mentions that suggest comparison
        has_btc = any(term in user_input.upper() for term in ['BTC', 'BITCOIN', 'BTCUSDT'])
        has_eth = any(term in user_input.upper() for term in ['ETH', 'ETHEREUM', 'ETHUSDT', 'ETHER'])
        
        # 🔥 FORCE COMPARISON: If user mentions both BTC and ETH
        if has_btc and has_eth:
            is_comparison_request = True
            logger.info("🔥 DETECTED: User mentioned both BTC and ETH - FORCING BTC vs ETH comparison mode")
        
        # 🔥 FORCE COMPARISON: Common comparison phrases
        comparison_phrases = [
            'btc vs eth', 'bitcoin vs ethereum', 'compare btc', 'compare bitcoin',
            'btc and eth', 'bitcoin and ethereum', 'show both', 'both crypto'
        ]
        
        if any(phrase in user_input.lower() for phrase in comparison_phrases):
            is_comparison_request = True
            logger.info("🔥 DETECTED: Comparison phrase found - FORCING BTC vs ETH comparison mode")

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
                    GuaranteedVisualChartTool(),  # 🎯 PRIMARY: BTC+ETH comparison charts
                    LiveCryptoDataTool(),
                    MarketStatusTool(),
                    HistoricalAnalysisTool()
                ]
                
                client_with_tools = client.bind_tools(tools)
                
                # Enhanced system message for BTC+ETH comparison charts
                messages_with_tools = [
                    SystemMessage(VISUAL_CHART_SYSTEM_PROMPT),
                    HumanMessage(user_input),
                ]
                
                if is_chart_request:
                    if is_comparison_request:
                        logger.info(f"🎯 BTC+ETH COMPARISON REQUEST DETECTED - Activating BTC vs ETH mode (timeframe: {detected_timeframe})")
                    else:
                        logger.info(f"🎯 SINGLE-LINE CHART REQUEST DETECTED - Activating standard mode (timeframe: {detected_timeframe})")
                
                # Process request
                result = client_with_tools.invoke(messages_with_tools)
                messages_with_tools.append(result)
                
                # Handle tool calls with enhanced parameter injection
                if result.tool_calls:
                    for tool_call in result.tool_calls:
                        logger.info(f"🔧 Tool call: {tool_call['name']}")
                        
                        # Special handling for chart generation
                        if tool_call["name"] == "generate_guaranteed_visual_chart":
                            # 🔥 AUTO-INJECT timeframe parameter
                            if 'timeframe' not in tool_call["args"] or tool_call["args"]["timeframe"] == "1D":
                                tool_call["args"]["timeframe"] = detected_timeframe
                                logger.info(f"🕐 AUTO-INJECTED timeframe: {detected_timeframe}")
                            
                            # 🔥 FORCE BTC+ETH comparison if detected
                            if is_comparison_request:
                                tool_call["args"]["comparison_request"] = True
                                tool_call["args"]["chart_type"] = "comparison"
                                
                                # Override any user-specified symbols to force BTC+ETH
                                tool_call["args"]["symbol"] = "BTC"
                                tool_call["args"]["additional_symbols"] = ["ETH"]
                                
                                logger.info(f"🔥 FORCED BTC+ETH COMPARISON: {tool_call['args']}")
                        
                        tool_found = None
                        for tool in tools:
                            if tool.name == tool_call["name"]:
                                tool_found = tool
                                break
                        
                        if tool_found:
                            try:
                                tool_result = tool_found._run(**tool_call["args"])
                                
                                # CONSOLE LOG: Tool result analysis
                                print(f"\n🔧 TOOL RESULT FROM {tool_call['name']}:")
                                print("="*60)
                                print(f"Timeframe: {tool_call['args'].get('timeframe', 'N/A')}")
                                print(f"Comparison: {tool_call['args'].get('comparison_request', False)}")
                                print(f"Contains ```graph: {'```graph' in tool_result}")
                                
                                # Analyze graph content for BTC+ETH
                                if '```graph' in tool_result:
                                    try:
                                        start = tool_result.find('```graph\n') + 9
                                        end = tool_result.find('\n```', start)
                                        graph_json = tool_result[start:end]
                                        graph_data = json.loads(graph_json)
                                        
                                        if 'history' in graph_data and 'content' in graph_data['history']:
                                            series_count = len(graph_data['history']['content'])
                                            title = graph_data['history'].get('title', 'No title')
                                            
                                            print(f"🔥 Chart Analysis:")
                                            print(f"  - Series count: {series_count}")
                                            print(f"  - Title: {title}")
                                            
                                            timeframe_labels = {
                                                '1m': '1-Minute',
                                                '5m': '5-Minute', 
                                                '15m': '15-Minute',
                                                '30m': '30-Minute', 
                                                '1h': 'Hourly',
                                                '4h': '4-Hour',
                                                '1D': 'Daily',
                                                '1d': 'Daily'
                                            }
                                            
                                            print(f"  - Timeframe in title: {detected_timeframe in title or timeframe_labels.get(detected_timeframe, '') in title}")
                                            
                                            if series_count >= 2:
                                                series_names = [s.get('name', 'Unknown') for s in graph_data['history']['content']]
                                                print(f"  - Series names: {series_names}")
                                                has_btc_eth = 'Bitcoin' in series_names and 'Ethereum' in series_names
                                                print(f"  - Has BTC+ETH: {has_btc_eth}")
                                    except Exception as parse_error:
                                        print(f"  - Graph parsing error: {parse_error}")
                                
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
                        # Check for graph content and determine chart type
                        if '```graph' in chunk.content:
                            chunk_data = {
                                "type": "chart",
                                "id": message_id,
                                "data": chunk.content,
                                "visual": True,
                                "interactive": True,
                                "multi_line": is_comparison_request,
                                "btc_eth_comparison": is_comparison_request,
                                "timeframe": detected_timeframe,
                                "weekly_view": is_weekly_request
                            }
                            logger.info(f"📊 CHART STREAMING TO FRONTEND: BTC+ETH={is_comparison_request}, timeframe={detected_timeframe}")
                        else:
                            chunk_data = {
                                "type": "text",
                                "id": message_id,
                                "data": chunk.content
                            }
                        
                        yield f'{json.dumps([chunk_data])}\n'

            except Exception as e:
                logger.error(f"Visual response generation error: {e}")
                import traceback
                traceback.print_exc()
                
                # Error response
                error_data = {
                    "type": "text", 
                    "id": f"error-{abs(hash(str(e))) % 1000}",
                    "data": f"❌ **System Error**: {str(e)}\n\nPlease try again or contact support."
                }
                
                yield f'{json.dumps([error_data])}\n'

        return StreamingResponse(
            generate_visual_response(),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Chart-Mode": "btc-eth-comparison-guaranteed",
                "X-Chart-Compatible": "chart-renderer-tsx",
                "X-Chart-Timeframe": detected_timeframe,
                "X-Comparison-Forced": str(is_comparison_request).lower()
            }
        )
        
    except Exception as e:
        logger.error(f"BTC+ETH comparison endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Direct Chart Generation Endpoints
@app.get("/generate/visual-chart/{symbol}")
async def direct_visual_chart(symbol: str, days_back: int = 30, timeframe: str = "1D"):
    """Direct endpoint for visual chart generation with timeframe support"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="Visual chart system not initialized")
        
        # Validate timeframe
        valid_timeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1D', '1d']
        if timeframe not in valid_timeframes:
            timeframe = '1D'
        
        logger.info(f"📊 Direct visual chart request: {symbol}, {days_back} days, {timeframe}")
        
        chart_tool = GuaranteedVisualChartTool()
        chart_result = chart_tool._run(symbol=symbol, days_back=days_back, timeframe=timeframe)
        
        # Extract chart data from the result
        if "```graph" in chart_result:
            chart_start = chart_result.find("```graph\n") + 9
            chart_end = chart_result.find("\n```", chart_start)
            chart_data_str = chart_result[chart_start:chart_end]
            chart_data = json.loads(chart_data_str)
            
            # CONSOLE LOG: Direct endpoint response
            print(f"\n📊 DIRECT CHART ENDPOINT RESPONSE FOR {symbol} ({timeframe}):")
            print("="*60)
            print(f"Chart data extracted: {len(chart_data_str)} characters")
            print(f"Valid JSON: {isinstance(chart_data, dict)}")
            print(f"Timeframe: {timeframe}")
            print("="*60 + "\n")
            
            return JSONResponse({
                "status": "success",
                "type": "visual_chart",
                "symbol": symbol,
                "days_back": days_back,
                "timeframe": timeframe,
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
async def direct_visual_comparison(request_data: dict):
    """Direct endpoint for visual comparison chart generation with timeframe support"""
    try:
        if not tool_manager or not tool_manager.tools_initialized:
            raise HTTPException(status_code=503, detail="Visual chart system not initialized")
        
        symbols = request_data.get('symbols', [])
        days_back = request_data.get('days_back', 90)
        timeframe = request_data.get('timeframe', '1D')
        
        if len(symbols) < 2:
            raise HTTPException(status_code=400, detail="Need at least 2 symbols for comparison")
        
        # Validate timeframe
        valid_timeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1D', '1d']
        if timeframe not in valid_timeframes:
            timeframe = '1D'
        
        logger.info(f"📊 Direct visual comparison: {symbols}, {days_back} days, {timeframe}")
        
        chart_tool = GuaranteedVisualChartTool()
        chart_result = chart_tool._run(
            symbol=symbols[0], 
            chart_type="comparison", 
            additional_symbols=symbols[1:], 
            days_back=days_back,
            timeframe=timeframe
        )
        
        # Extract chart data
        if "```graph" in chart_result:
            chart_start = chart_result.find("```graph\n") + 9
            chart_end = chart_result.find("\n```", chart_start)
            chart_data_str = chart_result[chart_start:chart_end]
            chart_data = json.loads(chart_data_str)
            
            # CONSOLE LOG: Direct comparison response
            print(f"\n📊 DIRECT COMPARISON ENDPOINT RESPONSE FOR {symbols} ({timeframe}):")
            print("="*70)
            print(f"Chart data extracted: {len(chart_data_str)} characters")
            print(f"Symbols count: {len(symbols)}")
            print(f"Timeframe: {timeframe}")
            print("="*70 + "\n")
            
            return JSONResponse({
                "status": "success",
                "type": "visual_comparison",
                "symbols": symbols,
                "days_back": days_back,
                "timeframe": timeframe,
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
    """Health check endpoint with detailed system status"""
    try:
        status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "version": "4.1.0",
            "system": "Visual Chart Crypto Analysis Server",
            "tools_initialized": bool(tool_manager and tool_manager.tools_initialized),
            "compatibility": {
                "frontend_component": "chart-renderer.tsx",
                "block_type": "```graph",
                "auto_detection": "language === 'graph'",
                "guaranteed_btc_eth_comparison": True,
                "timeframe_support": True
            },
            "features": [
                "Interactive visual charts",
                "Real-time cryptocurrency data",
                "BTC vs ETH comparison charts",
                "Multiple timeframe support",
                "Professional crypto styling",
                "Responsive design",
                "Console logging for debugging"
            ]
        }
        
        if tool_manager and tool_manager.tools_initialized:
            status["tools"] = {
                "tgx_market_data": "✅ Active",
                "jesse_historical": "✅ Active", 
                "jesse_chart_generator": "✅ Active",
                "guaranteed_visual_charts": "✅ Active"
            }
        else:
            status["tools"] = {
                "all_tools": "❌ Not initialized"
            }
            status["status"] = "degraded"
        
        return JSONResponse(status)
        
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }, status_code=500)

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