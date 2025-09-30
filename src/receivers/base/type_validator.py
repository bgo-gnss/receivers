"""Receiver type validation and auto-detection system.

This module provides intelligent detection of receiver types by analyzing
HTTP responses, FTP capabilities, and other receiver-specific characteristics.
Helps identify configuration mismatches and can suggest corrections.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple, Any
import requests
from urllib.parse import urljoin

class ReceiverTypeValidator:
    """Validates and detects receiver types through intelligent probing.
    
    Uses HTTP fingerprinting, FTP capabilities, and response analysis
    to determine the actual receiver type and compare with configuration.
    """
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize receiver type validator.
        
        Args:
            logger: Optional logger instance
        """
        self.logger = logger or logging.getLogger("receivers.validator")
        
        # HTTP signatures for different receiver types
        self.http_signatures = {
            'PolaRX5': {
                'endpoints': ['/'],
                'response_indicators': [
                    'septentrio',
                    'polarx',
                    'sbf',
                    'web interface'
                ],
                'headers': ['server'],
                'ports': [80, 8080, 443]
            },
            'NetR9': {
                'endpoints': ['/prog/show?Voltages', '/prog/show?sessions', '/'],
                'response_indicators': [
                    'trimble',
                    'netr9',
                    'voltage',
                    'trackingstatus'
                ],
                'headers': ['server'],
                'ports': [8060, 8061, 80]
            },
            'NetRS': {
                'endpoints': ['/prog/show?Voltages', '/prog/show?sessions', '/'],
                'response_indicators': [
                    'trimble',
                    'netrs',
                    'voltage',
                    'trackingstatus'
                ],
                'headers': ['server'],
                'ports': [8060, 8061, 80]
            },
            'G10': {
                'endpoints': ['/'],
                'response_indicators': [
                    'leica',
                    'gnss',
                    'system'
                ],
                'headers': ['server'],
                'ports': [80, 8080, 443]
            }
        }
    
    def validate_station_receiver_type(self, station_id: str, station_config: Dict[str, Any]) -> Dict[str, Any]:
        """Validate receiver type for a station through intelligent probing.
        
        Args:
            station_id: Station identifier
            station_config: Station configuration
            
        Returns:
            Dictionary with validation results and suggestions
        """
        self.logger.info(f"Validating receiver type for station {station_id}")
        
        # Get configured receiver type
        configured_type = None
        try:
            # Try different config structure patterns
            if 'receiver' in station_config and 'type' in station_config['receiver']:
                configured_type = station_config['receiver']['type']
            elif 'station' in station_config:
                # Check raw station data
                station_data = station_config['station']
                configured_type = station_data.get('receiver_type')
        except Exception as e:
            self.logger.warning(f"Could not extract configured receiver type: {e}")
        
        if not configured_type:
            return {
                'station_id': station_id,
                'validation_status': 'error',
                'error': 'No receiver type configured',
                'suggestion': 'Add receiver_type to station configuration'
            }
        
        # Get station IP for probing
        ip = station_config.get('router', {}).get('ip')
        if not ip:
            return {
                'station_id': station_id,
                'configured_type': configured_type,
                'validation_status': 'error',
                'error': 'No IP address available for probing'
            }
        
        # Probe receiver to determine actual type
        detected_types = self._probe_receiver(ip, station_id)
        
        # Analyze results
        if not detected_types:
            return {
                'station_id': station_id,
                'configured_type': configured_type,
                'ip': ip,
                'validation_status': 'unreachable',
                'detected_types': [],
                'error': 'Could not probe receiver - network unreachable or no HTTP response'
            }
        
        # Check if configured type matches detected types
        match_found = configured_type in detected_types
        
        result = {
            'station_id': station_id,
            'configured_type': configured_type,
            'detected_types': detected_types,
            'ip': ip,
            'validation_status': 'match' if match_found else 'mismatch',
            'confidence_scores': {dt: self._calculate_confidence(dt, detected_types) 
                                for dt in detected_types}
        }
        
        # Add suggestions for mismatches
        if not match_found:
            best_match = max(detected_types, key=lambda x: result['confidence_scores'][x])
            result['suggestion'] = {
                'recommended_type': best_match,
                'confidence': result['confidence_scores'][best_match],
                'action': f'Update station.cfg: receiver_type = {best_match}'
            }
            
            self.logger.warning(
                f"🚨 RECEIVER TYPE MISMATCH for {station_id}:\n"
                f"   Configured: {configured_type}\n" 
                f"   Detected: {', '.join(detected_types)}\n"
                f"   Recommended: {best_match} (confidence: {result['confidence_scores'][best_match]:.1%})"
            )
        else:
            self.logger.info(f"✅ Receiver type validation passed for {station_id}: {configured_type}")
        
        return result
    
    def _probe_receiver(self, ip: str, station_id: str) -> List[str]:
        """Probe receiver to detect actual type through HTTP fingerprinting.
        
        Args:
            ip: Receiver IP address
            station_id: Station identifier for logging
            
        Returns:
            List of possible receiver types based on probing
        """
        detected_types = []
        
        for receiver_type, signature in self.http_signatures.items():
            confidence = self._test_receiver_signature(ip, station_id, receiver_type, signature)
            if confidence > 0.3:  # Minimum confidence threshold
                detected_types.append(receiver_type)
        
        return detected_types
    
    def _test_receiver_signature(self, ip: str, station_id: str, receiver_type: str, 
                                signature: Dict[str, Any]) -> float:
        """Test if a receiver matches a specific type signature.
        
        Args:
            ip: Receiver IP address
            station_id: Station identifier  
            receiver_type: Type to test for
            signature: Signature configuration for this receiver type
            
        Returns:
            Confidence score (0.0 - 1.0) for this receiver type
        """
        confidence_factors = []
        
        # Test different HTTP ports
        for port in signature['ports']:
            try:
                base_url = f"http://{ip}:{port}"
                
                # Test endpoints
                for endpoint in signature['endpoints']:
                    try:
                        url = urljoin(base_url, endpoint)
                        response = requests.get(url, timeout=10)
                        
                        if response.status_code == 200:
                            # Check response content for indicators
                            content = response.text.lower()
                            headers = {k.lower(): v.lower() for k, v in response.headers.items()}
                            
                            # Score based on content indicators
                            content_score = self._score_content_match(content, signature['response_indicators'])
                            if content_score > 0:
                                confidence_factors.append(content_score)
                                self.logger.debug(
                                    f"{station_id}: {receiver_type} content match on {url} "
                                    f"(score: {content_score:.2f})"
                                )
                            
                            # Score based on headers
                            header_score = self._score_header_match(headers, signature.get('headers', []))
                            if header_score > 0:
                                confidence_factors.append(header_score * 0.5)  # Headers less reliable
                        
                        # Special case: Trimble receivers respond with specific HTTP structure
                        elif response.status_code == 404 and receiver_type in ['NetR9', 'NetRS']:
                            if '/prog/show?' in endpoint:
                                # 404 on /prog/show? might indicate wrong endpoint but right receiver type
                                confidence_factors.append(0.3)
                                self.logger.debug(f"{station_id}: {receiver_type} partial match on {url} (404)")
                    
                    except requests.exceptions.RequestException:
                        continue  # Try next endpoint/port
                        
            except Exception as e:
                self.logger.debug(f"{station_id}: Error testing {receiver_type} on {ip}:{port}: {e}")
                continue
        
        # Calculate overall confidence
        if not confidence_factors:
            return 0.0
        
        # Use weighted average of confidence factors
        return min(1.0, sum(confidence_factors) / len(confidence_factors))
    
    def _score_content_match(self, content: str, indicators: List[str]) -> float:
        """Score how well content matches receiver type indicators.
        
        Args:
            content: HTTP response content (lowercase)
            indicators: List of indicator strings to look for
            
        Returns:
            Match score (0.0 - 1.0)
        """
        matches = 0
        for indicator in indicators:
            if indicator.lower() in content:
                matches += 1
        
        return matches / len(indicators) if indicators else 0.0
    
    def _score_header_match(self, headers: Dict[str, str], header_keys: List[str]) -> float:
        """Score header matches for receiver type detection.
        
        Args:
            headers: HTTP response headers (lowercase)
            header_keys: Header keys to check
            
        Returns:
            Match score (0.0 - 1.0)
        """
        if not header_keys:
            return 0.0
        
        matches = 0
        for key in header_keys:
            if key.lower() in headers:
                # Could also check header values for receiver-specific strings
                matches += 1
        
        return matches / len(header_keys)
    
    def _calculate_confidence(self, receiver_type: str, detected_types: List[str]) -> float:
        """Calculate confidence score for a detected receiver type.
        
        Args:
            receiver_type: The receiver type to score
            detected_types: List of all detected types
            
        Returns:
            Confidence score (0.0 - 1.0)
        """
        # Simple scoring: higher confidence if fewer competing types detected
        base_confidence = 0.8
        competition_penalty = 0.1 * (len(detected_types) - 1)
        
        return max(0.5, base_confidence - competition_penalty)
    
    def batch_validate_stations(self, station_configs: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Validate receiver types for multiple stations.
        
        Args:
            station_configs: Dictionary mapping station_id to configuration
            
        Returns:
            Dictionary mapping station_id to validation results
        """
        results = {}
        
        self.logger.info(f"Starting batch validation of {len(station_configs)} stations")
        
        for station_id, config in station_configs.items():
            try:
                result = self.validate_station_receiver_type(station_id, config)
                results[station_id] = result
                
                # Log summary for each station
                if result['validation_status'] == 'mismatch':
                    suggestion = result.get('suggestion', {})
                    self.logger.warning(
                        f"❌ {station_id}: {result['configured_type']} → {suggestion.get('recommended_type')}"
                    )
                elif result['validation_status'] == 'match':
                    self.logger.info(f"✅ {station_id}: {result['configured_type']} verified")
                
            except Exception as e:
                self.logger.error(f"Validation failed for {station_id}: {e}")
                results[station_id] = {
                    'station_id': station_id,
                    'validation_status': 'error', 
                    'error': str(e)
                }
        
        return results
    
    def generate_correction_report(self, validation_results: Dict[str, Dict[str, Any]]) -> str:
        """Generate a report with suggested corrections for station.cfg.
        
        Args:
            validation_results: Results from batch_validate_stations
            
        Returns:
            Report string with suggested corrections
        """
        mismatches = [r for r in validation_results.values() 
                     if r.get('validation_status') == 'mismatch']
        
        if not mismatches:
            return "✅ All station receiver types are correctly configured!"
        
        report = f"🔧 RECEIVER TYPE CORRECTION REPORT\n"
        report += f"Found {len(mismatches)} stations with receiver type mismatches:\n\n"
        
        for result in mismatches:
            station_id = result['station_id']
            configured = result['configured_type']
            suggestion = result.get('suggestion', {})
            recommended = suggestion.get('recommended_type', 'Unknown')
            confidence = suggestion.get('confidence', 0)
            
            report += f"Station: {station_id}\n"
            report += f"  Current:     receiver_type = {configured}\n"
            report += f"  Suggested:   receiver_type = {recommended} (confidence: {confidence:.1%})\n"
            report += f"  Detected:    {', '.join(result.get('detected_types', []))}\n"
            report += f"  IP:          {result.get('ip', 'Unknown')}\n\n"
        
        report += "To apply corrections, update the corresponding entries in stations.cfg\n"
        
        return report