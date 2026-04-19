/**
 * Model configuration for Korvo AI integration
 * Contains supported models and their configurations
 */

const OPENROUTER_QWEN_MODELS = {
  'qwen/qwen3.6-plus': {
    name: 'Qwen 3.6 Plus',
    provider: 'openrouter',
    endpoint: 'https://openrouter.ai/api/v1/chat/completions',
    apiKeyHeader: 'Authorization',
    headers: {
      'HTTP-Referer': 'https://korvosystem.com',
      'X-Title': 'Korvo Voice Assistant'
    },
    maxTokens: 8192,
    pricing: {
      input: 0.00002,
      output: 0.00006,
      unit: "per_token"
    },
    capabilities: ['chat', 'reasoning', 'function_calling'],
    modality: 'text',
    description: 'High-performance Qwen 3.6 Plus model via OpenRouter'
  },
  'qwen/qwen3.6-plus:free': {
    name: 'Qwen 3.6 Plus (Free)',
    provider: 'openrouter',
    endpoint: 'https://openrouter.ai/api/v1/chat/completions',
    apiKeyHeader: 'Authorization',
    headers: {
      'HTTP-Referer': 'https://korvosystem.com',
      'X-Title': 'Korvo Voice Assistant'
    },
    maxTokens: 8192,
    pricing: {
      input: 0,
      output: 0,
      unit: "per_token"
    },
    capabilities: ['chat'],
    modality: 'text',
    description: 'Free variant of Qwen 3.6 Plus via OpenRouter'
  },
};

/**
 * Model manager for handling different AI models
 */
class ModelManager {
  constructor() {
    this.models = { ...OPENROUTER_QWEN_MODELS };
    this.defaultModel = 'qwen/qwen3.6-plus';
  }

  /**
   * Get available models
   */
  getModels() {
    return this.models;
  }

  /**
   * Get model configuration by ID
   */
  getModel(modelId) {
    return this.models[modelId] || null;
  }

  /**
   * Add a new model configuration
   */
  addModel(modelId, config) {
    this.models[modelId] = config;
  }

  /**
   * Remove a model
   */
  removeModel(modelId) {
    if (this.models[modelId]) {
      delete this.models[modelId];
      return true;
    }
    return false;
  }

  /**
   * Get the default model
   */
  getDefaultModel() {
    return this.getModel(this.defaultModel);
  }

  /**
   * Format messages for the OpenRouter API
   */
  formatMessages(transcript) {
    return [
      {
        role: "system",
        content: "You are a helpful, concise AI assistant integrated with a voice device. Respond naturally to voice interactions."
      },
      {
        role: "user",
        content: transcript
      }
    ];
  }
}

module.exports = {
  ModelManager,
  OPENROUTER_QWEN_MODELS
};