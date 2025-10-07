"""Slack Bot LangGraph demo using interrupt + Command resume pattern.

This version refactors the earlier conditional edge approach to leverage
`interrupt` (human-in-the-loop pauses) and `Command(resume=...)` to resume
execution. Conversation flow:

1. name_node -> interrupt asking for user's name
2. direction_node -> interrupt asking for left/right (re-prompts until valid)
3. outcome_node -> computes outcome and ends

State is persisted with an SQLite checkpointer so the FastAPI endpoint can
resume the flow by passing `Command(resume=message.text)`.
"""

from typing import TypedDict, Optional
import sqlite3
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from langgraph.constants import START
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

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
# Define the state schema (minimal fields we need)
# ---------------------------

class State(TypedDict, total=False):
    thread_ts: str              # Slack thread / conversation id
    name: str                   # Collected user name
    direction: str              # 'left' or 'right'
    outcome: str                # Final outcome narrative
    last_prompt: str            # Last prompt we showed to the user (for reference)

#############################
# Conversation Nodes (interrupt style)
#############################

def name_node(state: State):
    """Interrupt asking for the user's name if we don't have it yet."""
    if "name" not in state:
        prompt = "Hi there! What's your name?"
        print(f"Bot prompt (thread {state.get('thread_ts')}): {prompt}")
        # Pause graph; resume will supply the name (string)
        name_value = interrupt({"prompt": prompt})  # returns on resume
        # When resumed we have the user's input
        return {**state, "name": str(name_value).strip(), "last_prompt": prompt}
    return state


def direction_node(state: State):
    """Interrupt asking for direction until valid left/right received."""
    if "direction" in state:
        return state
    # First time asking for direction (name already collected)
    base_prompt = f"Nice to meet you, {state.get('name','friend')}! Which direction do you want to go? (left/right)"
    print(f"Bot prompt (thread {state.get('thread_ts')}): {base_prompt}")
    choice = interrupt({"prompt": base_prompt})
    # Validate, re-interrupt until valid
    dir_val = str(choice).strip().lower()
    while dir_val not in ("left", "right"):
        retry_prompt = "Please reply with 'left' or 'right'."
        print(f"Bot prompt (thread {state.get('thread_ts')}): {retry_prompt}")
        dir_val = str(interrupt({"prompt": retry_prompt})).strip().lower()
    return {**state, "direction": dir_val, "last_prompt": base_prompt}


def outcome_node(state: State):
    """Produce the final outcome message based on direction."""
    if state.get("outcome"):
        return state
    direction = state.get("direction")
    if direction == "left":
        msg = "You chose to go left and found a treasure! 🎉"
    else:
        msg = "You chose to go right and fell into a pit! 💥"
    print(f"Bot (thread {state.get('thread_ts')}): {msg}")
    return {**state, "outcome": msg, "bot_response": msg}


def left_node(state: State):
    msg = "You chose to go left and found a treasure! 🎉"
    print(f"Bot (thread {state.get('thread_ts')}): {msg}")
    return {**state, "outcome": msg, "bot_response": msg}

def right_node(state: State):
    msg = "You chose to go right and fell into a pit! 💥"
    print(f"Bot (thread {state.get('thread_ts')}): {msg}")
    return {**state, "outcome": msg, "bot_response": msg}


def thanks_for_playing_node(state: State):
    msg = "Thanks for playing! Goodbye!"
    print(f"Bot (thread {state.get('thread_ts')}): {msg}")
    return {**state, "outcome": msg, "bot_response": msg}

# ---------------------------
# Build the graph and FastAPI app
# ---------------------------

app = FastAPI(title="Slack Bot with LangGraph", description="Interrupt + Command workflow demo")

graph_builder = StateGraph(State)
graph_builder.add_node("name_node", name_node)
graph_builder.add_node("direction_node", direction_node)
graph_builder.add_node("outcome_node", outcome_node)
graph_builder.add_node("thanks_for_playing_node", thanks_for_playing_node)

graph_builder.add_node("left_node", left_node)
graph_builder.add_node("right_node", right_node)

graph_builder.add_edge(START, "name_node")
graph_builder.add_edge("name_node", "direction_node")

graph_builder.add_conditional_edges(
    "direction_node", 
    path=lambda state: state.get("direction") + "_node",
    path_map=["left_node", "right_node"],
)

graph_builder.add_edge("right_node", "thanks_for_playing_node")
graph_builder.add_edge("left_node", "thanks_for_playing_node")

graph_builder.add_edge("thanks_for_playing_node", "outcome_node")
graph_builder.add_edge("outcome_node", END)


checkpointer = SqliteSaver(sqlite3.connect("checkpoints.db", check_same_thread=False))
workflow = graph_builder.compile(checkpointer=checkpointer)

# Generate and save graph visualization trying pyppeteer method if available
try:
    graph_obj = workflow.get_graph()
    graph_image = graph_obj.draw_mermaid_png()
    label = "default"
    with open("workflow_graph.png", "wb") as f:
        f.write(graph_image)
    print(f"Graph visualization ({label}) saved as 'workflow_graph.png'")
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
    """Start or resume a conversation using interrupts.

    Algorithm:
    - If no state or completed (has outcome), start new workflow.
    - Else treat incoming text as a resume value: workflow.invoke(Command(resume=...)).
    - If result contains __interrupt__, return its prompt and status 'awaiting_input'.
    - Else return final outcome with status 'completed'.
    """
    config = {"configurable": {"thread_id": message.thread_ts}}
    try:
        try:
            current_state = workflow.get_state(config)
            state_values = current_state.values or {}
        except Exception:
            state_values = {}

        starting_new = not state_values or state_values.get("outcome")
        if starting_new:
            print(f"Starting (or restarting) conversation thread {message.thread_ts}")
            # Provide base thread id; first node will interrupt with name prompt
            result = workflow.invoke({"thread_ts": message.thread_ts}, config=config)
        else:
            # Resume with user input
            resume_text = message.text.strip()
            print(f"Resuming thread {message.thread_ts} with input: {resume_text}")
            result = workflow.invoke(Command(resume=resume_text), config=config)

        interrupts = result.get("__interrupt__")
        if interrupts:
            # Use first interrupt's prompt; store in state already via node
            intr_val = interrupts[0].value
            if isinstance(intr_val, dict):
                prompt = intr_val.get("prompt") or str(intr_val)
            else:
                prompt = str(intr_val)
            status = "awaiting_input"
            bot_message = prompt
        else:
            # Completed path
            bot_message = result.get("bot_response") or result.get("outcome") or "Done."
            status = "completed"

        print(f"Result state thread {message.thread_ts}: {result}")
        return SlackResponse(message=bot_message, status=status, thread_ts=message.thread_ts)
    except Exception as e:
        print(f"Error processing message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------
# Run the FastAPI server
# ---------------------------

if __name__ == "__main__":
    print("Starting Slack Bot FastAPI server...")
    print("Send messages to: http://localhost:8000/message")
    # import os
    # os.unlink("checkpoints.db") if os.path.exists("checkpoints.db") else None
    uvicorn.run(app, host="0.0.0.0", port=8000)
