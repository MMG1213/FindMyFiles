#!/bin/bash

# Start the FastMCP server in the background
echo "Starting FastMCP server on port 9006..."
python server.py &

# Wait for server to start
sleep 5

# Start Streamlit
echo "Starting Streamlit app..."
streamlit run app.py --server.port $PORT --server.address 0.0.0.0
