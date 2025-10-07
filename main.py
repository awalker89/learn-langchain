# filename: langgraph_pause_resume_demo.py

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from typing import TypedDict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sqlite3
import uvicorn

# ---------------------------
# FastAPI Models
# ---------------------------

class SlackMessage(BaseModel):
    text: str
    thread_ts: str
    user_id: Optional[str] = "user123"

class SlackResponse(BaseModel):
    message: str
    status: str
    thread_ts: str

# ---------------------------
# Define the state schema
# ---------------------------

class State(TypedDict):
    thread_ts: str
    user_response: Optional[str]
    step: Optional[str]
    user_id: Optional[str]
    bot_response: Optional[str]
    direction: Optional[str]

# ---------------------------
# Define our conversation nodes
# ---------------------------

def ask_name(state):
    """
    First node. Send a greeting and indicate we're waiting for response.
    """
    # If we've already collected a user_response or we're completed, don't re-greet
    if state.get("step") == "completed" or state.get("user_response"):
        # Pass through without changing bot_response so conditional logic can advance
        return state
    # Only greet if we are at start or not yet waiting
    if state.get("step") not in ("waiting_for_name",):
        response_text = "Hi there! What's your name?"
        print(f"Bot (thread {state.get('thread_ts')}): {response_text}")
        return {**state, "step": "waiting_for_name", "bot_response": response_text}
    # Already waiting, don't repeat message
    return state

def handle_name(state):
    """
    Second node. Called when we resume with user's reply.
    """
    name = state.get("user_response", "friend")
    response_text = f"Nice to meet you, {name}!"
    print(f"Bot (thread {state.get('thread_ts')}): {response_text}")
    # Transition to direction selection phase instead of completed
    return {**state, "step": "awaiting_direction", "bot_response": response_text, "user_response": None}

def ask_direction(state):
    """Prompt user to choose a direction (left/right)."""
    # Only prompt if we are awaiting_direction and no direction chosen yet
    if state.get("step") == "awaiting_direction" and not state.get("direction"):
        msg = "Do you go left or right?"
        print(f"Bot (thread {state.get('thread_ts')}): {msg}")
        return {**state, "bot_response": msg}
    return state

def handle_direction(state):
    """Handle the user's direction choice; re-ask if invalid."""
    if state.get("step") != "awaiting_direction":
        return state
    raw = (state.get("user_response") or "").strip().lower()
    if raw in ("left", "right"):
        msg = f"You went {raw}."
        print(f"Bot (thread {state.get('thread_ts')}): {msg}")
        return {**state, "step": "completed", "direction": raw, "bot_response": msg}
    # Invalid or empty choice -> ask again
    retry = "Please say 'left' or 'right'. Do you go left or right?"
    print(f"Bot (thread {state.get('thread_ts')}): {retry}")
    return {**state, "bot_response": retry, "user_response": None}

def should_continue(state):
    """
    Conditional function to determine next step
    """
    step = state.get("step")
    user_response = state.get("user_response")
    # Progression from asking name
    if step == "waiting_for_name" and user_response:
        return "handle_name"
    # After greeting, move to direction prompt phase
    if step == "awaiting_direction":
        # If we have a user_response attempt, process direction
        if user_response:
            return "handle_direction"
        # Otherwise keep asking direction
        return "ask_direction"
    # Default: pause (END placeholder) until external input arrives
    return "__end__"


# ---------------------------
# Build the graph and FastAPI app
# ---------------------------

app = FastAPI(title="Slack Bot with LangGraph", description="Pause/Resume workflow demo")

graph = StateGraph(State)

graph.add_node("ask_name", ask_name)
graph.add_node("handle_name", handle_name)

graph.set_entry_point("ask_name")
graph.add_conditional_edges("ask_name", should_continue)
graph.add_edge("handle_name", END)

# Compile with an SQLite checkpointer and interrupt points
checkpointer = SqliteSaver(sqlite3.connect("checkpoints.db", check_same_thread=False))
# No interrupt_before so that after user_response is injected, the graph can proceed to handle_name
workflow = graph.compile(checkpointer=checkpointer)

# Generate and save graph visualization
try:
    graph_image = workflow.get_graph().draw_mermaid_png()
    with open("workflow_graph.png", "wb") as f:
        f.write(graph_image)
    print("Graph visualization saved as 'workflow_graph.png'")
except Exception as e:
    print(f"Could not generate graph image: {e}")


# ---------------------------
# FastAPI Endpoints
# ---------------------------Resum

@app.get("/")
async def root():
    return {"message": "Slack Bot API is running", "endpoints": ["/message", "/status/{thread_ts}"]}

@app.post("/message", response_model=SlackResponse)
async def handle_slack_message(message: SlackMessage):
    """
    Handle incoming Slack messages and manage workflow state
    """
    try:
        config = {"configurable": {"thread_id": message.thread_ts}}
        
        # Attempt to fetch existing conversation state
        try:
            current_state = workflow.get_state(config)
            print("\n\n Fetched existing state: \n {current_state} \n\n\n")
            existing = bool(current_state.values)
        except Exception as e:
            print(f"Could not fetch state (treating as new): {e}")
            existing = False
            current_state = type("_Temp", (), {"values": {}})()

        if not existing or not current_state.values.get("step"):
            # New conversation start
            print(f"Starting new conversation for thread {message.thread_ts}")
            initial_state = {
                "thread_ts": message.thread_ts,
                "user_id": message.user_id,
                "step": "start"
            }
            result = workflow.invoke(initial_state, config=config)
            status = "started"
        else:
            # Existing conversation
            state_values = current_state.values
            print(f"Existing state for {message.thread_ts}: {state_values}")
            # If we are waiting for name and user provides it, attach user_response
            if state_values.get("step") == "waiting_for_name" and message.text.strip():
                updated_state = {**state_values, "user_response": message.text.strip(), "user_id": message.user_id}
                result = workflow.invoke(updated_state, config=config)
                status = "answered" if result.get("step") == "completed" else "resumed"
            else:
                # Nothing to progress; just re-invoke to possibly get greeting
                result = workflow.invoke(state_values, config=config)
                status = "awaiting_response" if state_values.get("step") == "waiting_for_name" else "resumed"

        bot_response = result.get("bot_response", "Processing...")
        print(f"Result for {message.thread_ts}: {result}")

        return SlackResponse(message=bot_response, status=status, thread_ts=message.thread_ts)
        
    except Exception as e:
        print(f"Error processing message: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status/{thread_ts}")
async def get_thread_status(thread_ts: str):
    """
    Get the current status of a conversation thread
    """
    try:
        config = {"configurable": {"thread_id": thread_ts}}
        current_state = workflow.get_state(config)
        
        if current_state.values:
            return {
                "thread_ts": thread_ts,
                "state": current_state.values,
                "next_action": current_state.next,
                "created_at": str(current_state.created_at) if current_state.created_at else None
            }
        else:
            return {"thread_ts": thread_ts, "state": "not_found"}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/threads")
async def list_active_threads():
    """
    List all active conversation threads
    """
    try:
        # This is a simple implementation - in a real app you'd want to track threads properly
        return {"message": "Check the database for active threads", "db_file": "checkpoints.db"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------
# Run the FastAPI server
# ---------------------------

if __name__ == "__main__":
    print("Starting Slack Bot FastAPI server...")
    print("API Documentation available at: http://localhost:8000/docs")
    print("Send messages to: http://localhost:8000/message")
    uvicorn.run(app, host="0.0.0.0", port=8000)
