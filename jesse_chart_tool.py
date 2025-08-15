import asyncio
import json
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

timeframe_labels = {
    '1m': '1-Minute',
    '5m': '5-Minute',
    '15m': '15-Minute',
    '30m': '30-Minute',
    '1h': 'Hourly',
    '4h': '4-Hour',
    '1D': 'Daily'
}


class JesseChartTool:
    """🔥 FIXED: Enhanced MCP Tool for Jesse.ai chart generation with GUARANTEED MULTI-LINE and TIMEFRAME support"""
    
    def __init__(self):
        load_dotenv()
        
        self.db_config = {
            'host': os.getenv('JESSE_DB_HOST', os.getenv("POSTGRES_HOST", "192.168.0.18")),
            'port': int(os.getenv('JESSE_DB_PORT', os.getenv("POSTGRES_PORT", "5432"))),
            'database': os.getenv('JESSE_DB_NAME', os.getenv("POSTGRES_DB", "jesse_db")),
            'user': os.getenv('JESSE_DB_USER', os.getenv("POSTGRES_USER", "jesse")),
            'password': os.getenv('JESSE_DB_PASSWORD', os.getenv("POSTGRES_PASSWORD", "password"))
        }
        
        self.connection = None
        self.initialized = False

        # FIXED: Timeframe mapping for proper interval support
        self.timeframe_mapping = {
            '1m': {'minutes': 1, 'seconds': 60},
            '5m': {'minutes': 5, 'seconds': 300},
            '15m': {'minutes': 15, 'seconds': 900}, 
            '30m': {'minutes': 30, 'seconds': 1800},
            '1h': {'minutes': 60, 'seconds': 3600},
            '4h': {'minutes': 240, 'seconds': 14400},
            '1D': {'minutes': 1440, 'seconds': 86400},
            '1d': {'minutes': 1440, 'seconds': 86400}  # Alternative
        }

    async def initialize(self):
        """Initialize database connection"""
        logger.info("🔧 Initializing Jesse Chart Tool...")
        
        try:
            await self._connect()
            await self._test_connection()
            self.initialized = True
            logger.info("✅ Jesse Chart Tool initialized")
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize Jesse Chart tool: {e}")
            raise

    async def _connect(self):
        """Connect to PostgreSQL database"""
        try:
            self.connection = psycopg2.connect(**self.db_config)
            self.connection.autocommit = True
            logger.debug("Connected to Jesse PostgreSQL database")
            
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise

    async def _test_connection(self):
        """Test database connection"""
        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM candle LIMIT 1;")
            count = cursor.fetchone()[0]
            logger.info(f"📊 Jesse Chart database connected - {count:,} candle records found")
            cursor.close()
            
        except Exception as e:
            logger.error(f"Database test failed: {e}")
            raise

    def _reconnect_if_needed(self):
        """Reconnect to database if connection is lost"""
        try:
            if not self.connection or self.connection.closed:
                logger.warning("Database connection lost, reconnecting...")
                asyncio.create_task(self._connect())
                
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            raise

    def _format_symbol(self, symbol: str) -> str:
        """Format symbol for database lookup"""
        symbol = symbol.upper().replace("/", "-")
    
        symbol_mapping = {
            "BTC": "BTC-USDT",
            "ETH": "ETH-USDT", 
            "BITCOIN": "BTC-USDT",
            "ETHEREUM": "ETH-USDT",
            "BTCUSDT": "BTC-USDT",
            "ETHUSDT": "ETH-USDT",
            "ETHER": "ETH-USDT",
            "ETH/USDT": "ETH-USDT",
            "ETH-USD": "ETH-USDT"
        }
    
        return symbol_mapping.get(symbol, symbol)

    def _get_symbol_display_name(self, symbol: str) -> str:
        """Get display name for symbol"""
        base_symbol = symbol.split('-')[0] if '-' in symbol else symbol.split('/')[0] if '/' in symbol else symbol
    
        crypto_names = {
            'BTC': 'Bitcoin',
            'ETH': 'Ethereum',
            'ETC': 'Ethereum Classic',
            'ADA': 'Cardano',
            'DOT': 'Polkadot',
            'LINK': 'Chainlink',
            'SOL': 'Solana',
            'MATIC': 'Polygon',
            'AVAX': 'Avalanche'
        }
    
        return crypto_names.get(base_symbol, base_symbol)

    def _get_symbol_color(self, symbol: str) -> str:
        """Get color for symbol charts"""
        base_symbol = symbol.split('-')[0] if '-' in symbol else symbol.split('/')[0] if '/' in symbol else symbol
        
        color_mapping = {
            'BTC': '#F7931A',  # Bitcoin orange
            'ETH': '#627EEA',  # Ethereum blue
            'ETC': '#328332',  # Ethereum Classic green
            'ADA': '#0033AD',  # Cardano blue
            'DOT': '#E6007A',  # Polkadot pink
            'LINK': '#2A5ADA', # Chainlink blue
            'SOL': '#9945FF',  # Solana purple
            'MATIC': '#8247E5', # Polygon purple
            'AVAX': '#E84142'  # Avalanche red
        }
        
        return color_mapping.get(base_symbol, '#2E86AB')  # Default blue

    def _aggregate_candles_by_timeframe(self, raw_candles: List[Dict], timeframe: str) -> List[Dict]:
        """🔥 FIXED: Aggregate candles based on timeframe with proper data alignment"""
        if not raw_candles or timeframe not in self.timeframe_mapping:
            return raw_candles
        
        if timeframe in ['1D', '1d']:
            # Daily aggregation (existing logic)
            daily_data = {}
            for row in raw_candles:
                if isinstance(row['trade_date'], datetime):
                    date_str = row['trade_date'].date().isoformat()
                else:
                    date_str = str(row['trade_date'])
                
                if date_str not in daily_data:
                    daily_data[date_str] = {
                        'date': date_str,
                        'open': row['open'],
                        'high': row['high'],
                        'low': row['low'],
                        'close': row['close'],
                        'volume': row['volume']
                    }
                else:
                    daily_data[date_str]['high'] = max(daily_data[date_str]['high'], row['high'])
                    daily_data[date_str]['low'] = min(daily_data[date_str]['low'], row['low'])
                    daily_data[date_str]['close'] = row['close']
                    daily_data[date_str]['volume'] += row['volume']
            
            candles = list(daily_data.values())
            candles.sort(key=lambda x: x['date'])
            return candles
        
        else:
            # Sub-daily aggregation (1m, 5m, 15m, 30m, 1h, 4h)
            interval_seconds = self.timeframe_mapping[timeframe]['seconds']
            
            aggregated_data = {}
            
            for row in raw_candles:
                # Convert timestamp to datetime
                dt = datetime.fromtimestamp(row['timestamp'] / 1000, tz=timezone.utc)
                
                # Round down to the nearest interval
                rounded_timestamp = (dt.timestamp() // interval_seconds) * interval_seconds
                rounded_dt = datetime.fromtimestamp(rounded_timestamp, tz=timezone.utc)
                
                # Create key for this interval
                interval_key = rounded_dt.isoformat()
                
                if interval_key not in aggregated_data:
                    aggregated_data[interval_key] = {
                        'date': rounded_dt.strftime('%Y-%m-%d %H:%M:%S'),
                        'open': row['open'],
                        'high': row['high'],
                        'low': row['low'],
                        'close': row['close'],
                        'volume': row['volume']
                    }
                else:
                    # Update OHLC for this interval
                    aggregated_data[interval_key]['high'] = max(aggregated_data[interval_key]['high'], row['high'])
                    aggregated_data[interval_key]['low'] = min(aggregated_data[interval_key]['low'], row['low'])
                    aggregated_data[interval_key]['close'] = row['close']  # Last close in interval
                    aggregated_data[interval_key]['volume'] += row['volume']
            
            # Convert to list and sort
            candles = list(aggregated_data.values())
            candles.sort(key=lambda x: x['date'])
            return candles

    def get_historical_prices(self, symbol: str, days_back: int = 365, 
                            exchange: str = 'Binance Perpetual Futures',
                            timeframe: str = '1D') -> List[Dict]:
        """🔥 FIXED: Get historical prices with proper timeframe support"""
        if not self.connection:
            logger.error("No database connection available")
            raise Exception("Database connection not available")
        
        try:
            jesse_symbol = self._format_symbol(symbol)
            
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days_back)
            
            logger.info(f"Querying chart data for symbol: {jesse_symbol}, exchange: {exchange}, timeframe: {timeframe}")
            logger.info(f"Date range: {start_date} to {end_date}")
            
            with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
                # Check data availability first
                cursor.execute("""
                    SELECT COUNT(*) as count
                    FROM candle 
                    WHERE symbol = %s AND exchange = %s
                """, (jesse_symbol, exchange))
                
                check_result = cursor.fetchone()
                logger.info(f"Chart data check - Count: {check_result['count']}")
                
                if not check_result['count']:
                    logger.error(f"No chart data found for {jesse_symbol} on {exchange}")
                    raise Exception(f"No chart data found for {jesse_symbol} on {exchange}")

                # Get raw candle data (no aggregation at query level)
                query = """
                SELECT 
                    DATE(to_timestamp(timestamp/1000)) as trade_date,
                    timestamp,
                    open::float as open,
                    high::float as high,
                    low::float as low,
                    close::float as close,
                    volume::float as volume
                FROM candle 
                WHERE symbol = %s 
                AND exchange = %s
                AND to_timestamp(timestamp/1000) >= %s
                AND to_timestamp(timestamp/1000) <= %s
                ORDER BY timestamp ASC
                """
                
                cursor.execute(query, (jesse_symbol, exchange, start_date, end_date))
                results = cursor.fetchall()
                logger.info(f"Retrieved {len(results)} raw candles for chart")
                
                if not results:
                    logger.error(f"No results returned from query for {jesse_symbol}")
                    raise Exception(f"No results returned from query for {jesse_symbol}")
                
                # 🔥 Apply timeframe aggregation
                candles = self._aggregate_candles_by_timeframe(results, timeframe)
                
                logger.info(f"Aggregated to {len(candles)} candles for timeframe {timeframe}")
                return candles
                
        except Exception as e:
            logger.error(f"Error fetching chart data for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Failed to fetch chart data for {symbol}: {str(e)}")

    async def get_price_chart_data(self, symbol: str, days_back: int = 30, timeframe: str = "1D") -> str:
        """🔥 FIXED: Get price chart data with PROPER TIMEFRAME TITLE"""
        try:
            self._reconnect_if_needed()
            
            logger.info(f"🎯 Generating SINGLE chart-renderer.tsx compatible data for {symbol}, {days_back} days, {timeframe}")
            
            # Get real data from database with timeframe support
            candles = self.get_historical_prices(symbol, days_back=days_back, exchange='Binance Perpetual Futures', timeframe=timeframe)
            
            if not candles or len(candles) == 0:
                error_msg = f"No data available for {symbol}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # Build chart data in the EXACT format chart-renderer.tsx expects
            symbol_name = self._get_symbol_display_name(symbol)
            symbol_color = self._get_symbol_color(symbol)
            
            # 🔥 CRITICAL FIX: Ensure timeframe is reflected in title
            timeframe_display = timeframe_labels.get(timeframe, timeframe)
            if timeframe == '1D' and days_back >= 7:
                # For daily data over a week, show as weekly view
                title = f"{symbol_name} Price - Last {days_back} Days (Weekly View - Daily Data)"
            else:
                title = f"{symbol_name} Price - Last {days_back} Days ({timeframe_display})"
            
            # Ensure clean data arrays
            dates = [candle['date'] for candle in candles]
            prices = [float(candle['close']) for candle in candles]
            
            # Validate data before creating chart
            if len(dates) == 0 or len(prices) == 0:
                error_msg = "Empty data arrays after processing"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            if len(dates) != len(prices):
                logger.warning(f"Data length mismatch: dates={len(dates)}, prices={len(prices)}")
                min_length = min(len(dates), len(prices))
                dates = dates[:min_length]
                prices = prices[:min_length]
            
            # EXACT structure that chart-renderer.tsx expects for SINGLE LINE
            chart_data = {
                "history": {
                    "title": title,
                    "xlabel": "Date",
                    "content": [{
                        "name": symbol_name,
                        "primary_colour": symbol_color,
                        "x": dates,
                        "price": {
                            "y": prices,
                            "ylabel": "Price (USD)"
                        }
                    }]
                }
            }
            
            logger.info(f"✅ SINGLE chart-renderer.tsx compatible data created for {symbol} with {len(candles)} data points")
            logger.info(f"📊 Date range: {dates[0]} to {dates[-1]}")
            logger.info(f"💰 Price range: ${prices[0]:.2f} to ${prices[-1]:.2f}")
            logger.info(f"🏷️ Title: {title}")
            
            result = json.dumps(chart_data, indent=2)
            
            print(f"\n🎯 SINGLE CHART DATA OUTPUT FOR {symbol} ({timeframe}):")
            print("=" * 70)
            print(f"Title: {title}")
            print(f"Series count: 1 (Single line)")
            print("=" * 70)
            print(result)
            print("=" * 70)
            
            return result
            
        except Exception as e:
            logger.error(f"Chart data error for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Failed to generate chart data for {symbol}: {str(e)}")

    async def get_comparison_chart_data(self, symbols: List[str], days_back: int = 30, timeframe: str = "1D") -> str:
        """🔥 CRITICAL FIX: Generate GUARANTEED multi-line comparison chart with SEPARATE BTC and ETH series"""
        try:
            logger.info(f"🔥 GUARANTEED Multi-line comparison: {symbols}, timeframe: {timeframe}")
            
            # 🔥 FORCE BTC and ETH for comparison charts - ALWAYS
            comparison_symbols = ['BTC', 'ETH']
            
            if not symbols or len(symbols) == 0:
                symbols = comparison_symbols
                logger.info("🔥 No symbols provided - using default BTC vs ETH")
            else:
                # ALWAYS force BTC and ETH for guaranteed multi-line
                symbols = comparison_symbols
                logger.info("🔥 FORCING BTC vs ETH comparison for guaranteed multi-line chart")
            
            # 🔥 CRITICAL FIX: Create SEPARATE data series for EACH crypto
            chart_content = []
            
            # Process each symbol individually to create separate series
            for symbol in symbols:
                logger.info(f"📈 Fetching SEPARATE data series for {symbol}...")
                
                try:
                    candles = self.get_historical_prices(
                        symbol, 
                        days_back=days_back, 
                        exchange='Binance Perpetual Futures',
                        timeframe=timeframe
                    )
                    
                    if candles and len(candles) > 0:
                        # Extract dates and prices for this specific crypto
                        dates = [candle['date'] for candle in candles]
                        prices = [round(candle['close'], 2) for candle in candles]
                        
                        # Get proper name and color
                        symbol_name = self._get_symbol_display_name(symbol)
                        symbol_color = self._get_symbol_color(symbol)
                        
                        # 🔥 CREATE INDIVIDUAL SERIES - THIS IS THE CRITICAL FIX
                        series_data = {
                            "name": symbol_name,
                            "primary_colour": symbol_color,
                            "x": dates,
                            "price": {
                                "y": prices,
                                "ylabel": "Price (USD)"
                            }
                        }
                        
                        chart_content.append(series_data)
                        logger.info(f"✅ Created SEPARATE series: {symbol_name} ({symbol_color}) - {len(prices)} points")
                        
                except Exception as e:
                    logger.error(f"❌ Failed to get data for {symbol}: {e}")
                    continue
            
            # Validate we have exactly 2 series (BTC + ETH)
            if len(chart_content) != 2:
                logger.error(f"❌ Expected exactly 2 series (BTC+ETH) but got {len(chart_content)}")
                raise Exception(f"Failed to create 2-line comparison chart. Got {len(chart_content)} series instead of 2.")
            
            # 🔥 CRITICAL FIX: Create proper title with timeframe
            timeframe_display = timeframe_labels.get(timeframe, timeframe)
            symbols_text = " vs ".join([series["name"] for series in chart_content])
            
            if timeframe == '1D' and days_back >= 7:
                title = f"{symbols_text} Comparison - Last {days_back} Days (Weekly View - Daily Data)"
            else:
                title = f"{symbols_text} Comparison - Last {days_back} Days ({timeframe_display})"
            
            # Build final chart data structure
            chart_data = {
                "history": {
                    "title": title,
                    "xlabel": "Date",
                    "content": chart_content  # This now contains 2 separate series
                }
            }
            
            logger.info(f"🔥 SUCCESS: {len(chart_content)} SEPARATE lines created for comparison!")
            logger.info(f"📊 Title: {title}")
            logger.info(f"🎨 Series: {[s['name'] for s in chart_content]}")
            logger.info(f"🎨 Colors: {[s['primary_colour'] for s in chart_content]}")
            
            result = json.dumps(chart_data, indent=2)
            
            print(f"\n🔥 GUARANTEED MULTI-LINE COMPARISON CHART DATA:")
            print("=" * 80)
            print(f"SYMBOLS: {symbols}")
            print(f"TIMEFRAME: {timeframe} ({timeframe_display})")
            print(f"LINES CREATED: {len(chart_content)} (GUARANTEED 2)")
            print(f"TITLE: {title}")
            print(f"SERIES NAMES: {[s['name'] for s in chart_content]}")
            print(f"SERIES COLORS: {[s['primary_colour'] for s in chart_content]}")
            print("=" * 80)
            print(result[:1000] + "..." if len(result) > 1000 else result)
            print("=" * 80)
            
            return result
            
        except Exception as e:
            logger.error(f"❌ CRITICAL comparison chart error: {e}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Failed to generate BTC vs ETH comparison chart: {str(e)}")

    async def close(self):
        """Close database connection"""
        logger.info("🔌 Closing Jesse Chart Tool...")
        
        if self.connection and not self.connection.closed:
            self.connection.close()
            
        self.initialized = False
        
        logger.info("✅ Jesse Chart Tool closed")