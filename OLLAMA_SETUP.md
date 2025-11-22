# Ollama Setup Guide (Free, Local LLM)

This system now uses **Ollama** for free, local LLM inference instead of paid OpenAI API.

## What is Ollama?

Ollama is a tool that runs large language models locally on your machine - completely free! No API keys, no quotas, no costs.

## Installation Steps

### 1. Install Ollama

**macOS:**
```bash
brew install ollama
```

**Or download from:**
https://ollama.ai/download

### 2. Start Ollama Server

```bash
ollama serve
```

This will start the Ollama server on `http://localhost:11434`

### 3. Download a Model

Download a model (this is a one-time download, ~2-4GB):

```bash
# Recommended: Llama 3.2 (good balance of speed and quality)
ollama pull llama3.2

# Or try other models:
# ollama pull mistral      # Fast and efficient
# ollama pull llama3.1     # Larger, more capable
# ollama pull qwen2.5      # Great for code and reasoning
```

### 4. Verify Installation

Test that Ollama is working:

```bash
ollama run llama3.2 "Hello, how are you?"
```

If you get a response, Ollama is working! ✅

## Configuration

The system is already configured to use Ollama. Default settings:

- **Base URL**: `http://localhost:11434` (or `http://host.docker.internal:11434` in Docker)
- **Model**: `llama3.2`

You can customize these in `.env`:

```bash
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

## Running with Docker

If you're running the API in Docker, Ollama needs to run on your host machine (not in Docker).

1. **Start Ollama on your host:**
   ```bash
   ollama serve
   ```

2. **The Docker container will connect to `host.docker.internal:11434`**

   This is already configured in `docker-compose.yml`.

## Troubleshooting

### "Ollama server not running"
- Make sure `ollama serve` is running
- Check that it's listening on port 11434: `curl http://localhost:11434/api/tags`

### "Model not found"
- Download the model: `ollama pull llama3.2`
- Or change `OLLAMA_MODEL` in `.env` to a model you have

### "Connection refused" in Docker
- Make sure Ollama is running on your host machine
- The Docker container uses `host.docker.internal:11434` to connect to your host

## Benefits of Ollama

✅ **Completely Free** - No API costs, no quotas  
✅ **Privacy** - All processing happens locally  
✅ **Fast** - No network latency  
✅ **Offline** - Works without internet  
✅ **No Limits** - Use as much as you want  

## Model Recommendations

- **llama3.2** (default) - Good balance, ~2GB
- **mistral** - Fast and efficient, ~4GB
- **llama3.1** - More capable, ~4.7GB
- **qwen2.5** - Great for reasoning, ~4.4GB

Choose based on your needs and available RAM!

