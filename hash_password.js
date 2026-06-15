// hash_password.js
import bcrypt from 'bcryptjs';
const password = process.argv[2];
console.log(bcrypt.hashSync(password, 10));