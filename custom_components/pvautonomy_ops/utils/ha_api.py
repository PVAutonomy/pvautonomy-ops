"""Home Assistant state reader utility.

Provides methods to read entity states and attributes.
"""
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)


class HomeAssistantStateReader:
    """Helper class to read HA entity states."""

    def __init__(self, hass: HomeAssistant):
        """Initialize state reader.
        
        Args:
            hass: Home Assistant instance
        """
        self.hass = hass

    async def get_state(self, entity_id: str) -> State | None:
        """Get entity state from HA.
        
        Args:
            entity_id: Entity ID to read
            
        Returns:
            State object or None if not found
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.debug("Entity not found: %s", entity_id)
        return state

    async def get_state_value(self, entity_id: str, default: Any = None) -> Any:
        """Get entity state value.
        
        Args:
            entity_id: Entity ID to read
            default: Default value if entity not found or unavailable
            
        Returns:
            State value or default
        """
        state = await self.get_state(entity_id)
        if state is None or state.state in ["unknown", "unavailable"]:
            return default
        return state.state

    async def get_attribute(
        self, entity_id: str, attribute: str, default: Any = None
    ) -> Any:
        """Get entity attribute value.
        
        Args:
            entity_id: Entity ID to read
            attribute: Attribute name
            default: Default value if not found
            
        Returns:
            Attribute value or default
        """
        state = await self.get_state(entity_id)
        if state is None:
            return default
        return state.attributes.get(attribute, default)

    async def get_states_by_pattern(self, pattern: str) -> list[State]:
        """Get all entities matching pattern.
        
        Args:
            pattern: Entity ID pattern (e.g., 'sensor.sph10k_*')
            
        Returns:
            List of matching State objects
        """
        # Simple pattern matching using startswith
        if "*" in pattern:
            prefix = pattern.split("*")[0]
            return [
                state
                for entity_id, state in self.hass.states.async_all()
                if entity_id.startswith(prefix)
            ]
        else:
            # Exact match
            state = await self.get_state(pattern)
            return [state] if state is not None else []

    async def entity_exists(self, entity_id: str) -> bool:
        """Check if entity exists in HA.
        
        Args:
            entity_id: Entity ID to check
            
        Returns:
            True if entity exists, False otherwise
        """
        return self.hass.states.get(entity_id) is not None

    async def is_available(self, entity_id: str) -> bool:
        """Check if entity is available (not unknown/unavailable).
        
        Args:
            entity_id: Entity ID to check
            
        Returns:
            True if entity is available, False otherwise
        """
        state = await self.get_state(entity_id)
        if state is None:
            return False
        return state.state not in ["unknown", "unavailable"]
