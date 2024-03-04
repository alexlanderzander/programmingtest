function caesarCipher(str, key, action) {
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    let result = '';

    for (let i = 0; i < str.length; i++) {
        let currentLetter = str[i];
        let index = alphabet.indexOf(currentLetter);
        if (index === -1) {
           
            result += currentLetter;
        } else {
            if (action === 'encode') {
                index = (index + key) % 26;
            } else if (action === 'decode') {
                index = (index - key + 26) % 26;
            }
            result += alphabet[index];
        }
    }

    return result;
}

const action = process.argv[2]; // 'encode' oder 'decode'
const text = process.argv[3]; // Text
const key = parseInt(process.argv[4], 10); // Schlüssel

if (!action || !text || isNaN(key)) {
    console.log('Usage: node index.js <encode|decode> <text> <key>');
} else {
    console.log(caesarCipher(text, key, action));
}
