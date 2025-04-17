from __future__ import annotations

import logging
from typing import List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
	BaseMessage,
	HumanMessage,
)
from langchain_core.messages.utils import convert_to_openai_messages
from mem0 import Memory as Mem0Memory

from browser_use.agent.message_manager.service import MessageManager
from browser_use.agent.message_manager.views import ManagedMessage, MessageMetadata
from browser_use.utils import time_execution_sync
from browser_use.agent.memory.views import MemoryConfig

logger = logging.getLogger(__name__)

class Memory:
	"""
	Manages procedural memory for agents.

	This class implements a procedural memory management system using Mem0 that transforms agent interaction history
	into concise, structured representations at specified intervals. It serves to optimize context window
	utilization during extended task execution by converting verbose historical information into compact,
	yet comprehensive memory constructs that preserve essential operational knowledge.
	"""

	def __init__(
		self,
		message_manager: MessageManager,
		llm: BaseChatModel,
		config: Optional[MemoryConfig] = None,
	):
		self.message_manager = message_manager
		self.llm = llm

		# Initialize configuration with defaults based on the LLM if not provided
		if config is None:
			config = MemoryConfig(
				llm_instance=llm,
				agent_id=f"agent_{id(self)}"
			)

			# Set appropriate embedder based on LLM type
			llm_class = llm.__class__.__name__
			if llm_class == 'ChatOpenAI':
				config.embedder_provider = 'openai'
				config.embedder_model = 'text-embedding-3-small'
				config.embedder_dims = 1536
			elif llm_class == 'ChatGoogleGenerativeAI':
				config.embedder_provider = 'gemini'
				config.embedder_model = 'models/text-embedding-004'
				config.embedder_dims = 768
			elif llm_class == 'ChatOllama':
				config.embedder_provider = 'ollama'
				config.embedder_model = 'nomic-embed-text'
				config.embedder_dims = 512
		else:
			# Ensure LLM instance is set in the config
			config.llm_instance = llm

		self.config = config

		# Check if sentence_transformers is needed
		if config.embedder_provider == 'huggingface':
			try:
				from sentence_transformers import SentenceTransformer
			except ImportError:
				raise ImportError(f'sentence_transformers is required for managing memory with huggingface embeddings. Please install it with `pip install sentence-transformers`.')

		# Initialize Mem0 with the configuration
		self.mem0 = Mem0Memory.from_config(config_dict=config.full_config_dict)

	@time_execution_sync('--create_procedural_memory')
	def create_procedural_memory(self, current_step: int) -> None:
		"""
		Create a procedural memory if needed based on the current step.

		Args:
		    current_step: The current step number of the agent
		"""
		logger.info(f'Creating procedural memory at step {current_step}')

		# Get all messages
		all_messages = self.message_manager.state.history.messages

		# Separate messages into those to keep as-is and those to process for memory
		new_messages = []
		messages_to_process = []

		for msg in all_messages:
			if isinstance(msg, ManagedMessage) and msg.metadata.message_type in {'init', 'memory'}:
				# Keep system and memory messages as they are
				new_messages.append(msg)
			else:
				if len(msg.message.content) > 0:
					messages_to_process.append(msg)

		# Need at least 2 messages to create a meaningful summary
		if len(messages_to_process) <= 1:
			logger.info('Not enough non-memory messages to summarize')
			return
		# Create a procedural memory
		memory_content = self._create([m.message for m in messages_to_process], current_step)

		if not memory_content:
			logger.warning('Failed to create procedural memory')
			return

		# Replace the processed messages with the consolidated memory
		memory_message = HumanMessage(content=memory_content)
		memory_tokens = self.message_manager._count_tokens(memory_message)
		memory_metadata = MessageMetadata(tokens=memory_tokens, message_type='memory')

		# Calculate the total tokens being removed
		removed_tokens = sum(m.metadata.tokens for m in messages_to_process)

		# Add the memory message
		new_messages.append(ManagedMessage(message=memory_message, metadata=memory_metadata))

		# Update the history
		self.message_manager.state.history.messages = new_messages
		self.message_manager.state.history.current_tokens -= removed_tokens
		self.message_manager.state.history.current_tokens += memory_tokens
		logger.info(f'Messages consolidated: {len(messages_to_process)} messages converted to procedural memory')

	def _create(self, messages: List[BaseMessage], current_step: int) -> Optional[str]:
		parsed_messages = convert_to_openai_messages(messages)
		try:
			results = self.mem0.add(
				messages=parsed_messages,
				agent_id=self.config.agent_id,
				memory_type='procedural_memory',
				metadata={'step': current_step},
			)
			if len(results.get('results', [])):
				return results.get('results', [])[0].get('memory')
			return None
		except Exception as e:
			logger.error(f'Error creating procedural memory: {e}')
			return None
