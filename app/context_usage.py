"""Request usage metadata only: never persist prompts, images or credentials here."""
import math


def estimate(messages):
    chars = 0
    images = 0
    for message in messages:
        content = message.get('content', '')
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if part.get('type') == 'text':
                    chars += len(str(part.get('text', '')))
                elif part.get('type') == 'image_url':
                    images += 1
    return {'kind': 'estimate', 'input_tokens': math.ceil(chars / 2) + len(messages) * 8,
            'output_tokens': None, 'total_tokens': None, 'images': images,
            'message_count': len(messages), 'text_chars': chars}


def reported(estimate, response):
    result = dict(estimate)
    usage = response.get('usage') if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return result
    value = usage.get('prompt_tokens', usage.get('input_tokens'))
    if type(value) is not int or value < 0:
        return result
    result.update(kind='reported', input_tokens=value)
    output = usage.get('completion_tokens', usage.get('output_tokens'))
    if type(output) is int and output >= 0:
        result.update(output_tokens=output, total_tokens=value + output)
    return result
